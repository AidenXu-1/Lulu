#include "lulu_agent_xpc_bridge.h"

#include <bsm/libbsm.h>
#include <dispatch/dispatch.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <libproc.h>
#include <mach/message.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <xpc/xpc.h>

enum {
    LULU_XPC_OK = 0,
    LULU_XPC_INVALID_ARGUMENT = 2,
    LULU_XPC_AUDIT_TOKEN_UNAVAILABLE = 10,
    LULU_XPC_PEER_MISMATCH = 11,
    LULU_XPC_PROCESS_UNAVAILABLE = 12,
    LULU_XPC_CONNECTION_FAILED = 20,
    LULU_XPC_TIMEOUT = 21,
    LULU_XPC_PROTOCOL_ERROR = 22,
    LULU_XPC_HANDLER_ERROR = 30,
};

typedef void (*xpc_connection_get_audit_token_fn)(
    xpc_connection_t connection,
    audit_token_t *token
);

typedef struct {
    xpc_connection_t connection;
} lulu_xpc_client_v1;

static lulu_xpc_request_handler_v1 server_handler = NULL;
static void *server_context = NULL;

static xpc_connection_get_audit_token_fn audit_token_function(void) {
    return (xpc_connection_get_audit_token_fn)dlsym(
        RTLD_DEFAULT,
        "xpc_connection_get_audit_token"
    );
}

int lulu_xpc_runtime_available(void) {
    return audit_token_function() == NULL ? 0 : 1;
}

uint32_t lulu_xpc_current_audit_session(void) {
    struct auditinfo_addr info;
    memset(&info, 0, sizeof(info));
    if (getaudit_addr(&info, (int)sizeof(info)) != 0) {
        return 0;
    }
    return (uint32_t)info.ai_asid;
}

static int copy_peer_facts(
    xpc_connection_t connection,
    lulu_xpc_peer_facts_v1 *output
) {
    if (connection == NULL || output == NULL) {
        return LULU_XPC_INVALID_ARGUMENT;
    }
    xpc_connection_get_audit_token_fn get_token = audit_token_function();
    if (get_token == NULL) {
        return LULU_XPC_AUDIT_TOKEN_UNAVAILABLE;
    }
    audit_token_t token = INVALID_AUDIT_TOKEN_VALUE;
    get_token(connection, &token);
    pid_t pid = audit_token_to_pid(token);
    uid_t euid = audit_token_to_euid(token);
    gid_t egid = audit_token_to_egid(token);
    au_id_t auid = audit_token_to_auid(token);
    au_asid_t asid = audit_token_to_asid(token);
    int pidversion = audit_token_to_pidversion(token);
    if (
        pid <= 0 || pidversion <= 0 ||
        pid != xpc_connection_get_pid(connection) ||
        euid != xpc_connection_get_euid(connection) ||
        egid != xpc_connection_get_egid(connection) ||
        asid != xpc_connection_get_asid(connection)
    ) {
        return LULU_XPC_PEER_MISMATCH;
    }
    char path[LULU_XPC_PATH_MAX] = {0};
    int length = proc_pidpath_audittoken(&token, path, sizeof(path));
    if (length <= 0 || length >= (int)sizeof(path)) {
        return LULU_XPC_PROCESS_UNAVAILABLE;
    }
    struct stat before;
    if (lstat(path, &before) != 0 || !S_ISREG(before.st_mode) || S_ISLNK(before.st_mode)) {
        return LULU_XPC_PROCESS_UNAVAILABLE;
    }
    int descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
        return LULU_XPC_PROCESS_UNAVAILABLE;
    }
    struct stat current;
    int stat_result = fstat(descriptor, &current);
    close(descriptor);
    if (
        stat_result != 0 || before.st_dev != current.st_dev ||
        before.st_ino != current.st_ino || before.st_mode != current.st_mode ||
        before.st_size != current.st_size
    ) {
        return LULU_XPC_PROCESS_UNAVAILABLE;
    }
    memset(output, 0, sizeof(*output));
    output->schema_version = 1;
    output->pid = pid;
    output->effective_uid = euid;
    output->effective_gid = egid;
    output->audit_uid = auid;
    output->audit_session_id = (uint32_t)asid;
    output->pidversion = pidversion;
    output->device = (uint64_t)current.st_dev;
    output->inode = (uint64_t)current.st_ino;
    output->size = (uint64_t)current.st_size;
    output->mode = (uint32_t)(current.st_mode & 07777);
    output->connection_instance = (uint64_t)(uintptr_t)connection;
    memcpy(output->audit_token, &token, sizeof(token));
    memcpy(output->executable_path, path, (size_t)length + 1);
    return LULU_XPC_OK;
}

void *lulu_xpc_client_open(const char *service_name) {
    if (service_name == NULL || service_name[0] == '\0' || !lulu_xpc_runtime_available()) {
        return NULL;
    }
    lulu_xpc_client_v1 *client = calloc(1, sizeof(*client));
    if (client == NULL) {
        return NULL;
    }
    client->connection = xpc_connection_create_mach_service(service_name, NULL, 0);
    if (client->connection == NULL) {
        free(client);
        return NULL;
    }
    xpc_connection_set_event_handler(client->connection, ^(xpc_object_t event) {
        (void)event;
    });
    xpc_connection_activate(client->connection);
    return client;
}

int lulu_xpc_client_exchange(
    void *client_handle,
    const char *request_json,
    char *response_json,
    size_t response_capacity,
    lulu_xpc_peer_facts_v1 *server_peer,
    uint32_t timeout_milliseconds
) {
    if (
        client_handle == NULL || request_json == NULL || response_json == NULL ||
        response_capacity < 2 || server_peer == NULL || timeout_milliseconds == 0
    ) {
        return LULU_XPC_INVALID_ARGUMENT;
    }
    lulu_xpc_client_v1 *client = client_handle;
    xpc_object_t message = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_uint64(message, "schema_version", 1);
    xpc_dictionary_set_string(message, "payload", request_json);
    dispatch_semaphore_t completed = dispatch_semaphore_create(0);
    __block xpc_object_t reply = NULL;
    xpc_connection_send_message_with_reply(
        client->connection,
        message,
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0),
        ^(xpc_object_t received) {
            reply = xpc_retain(received);
            dispatch_semaphore_signal(completed);
        }
    );
    xpc_release(message);
    dispatch_time_t deadline = dispatch_time(
        DISPATCH_TIME_NOW,
        (int64_t)timeout_milliseconds * NSEC_PER_MSEC
    );
    if (dispatch_semaphore_wait(completed, deadline) != 0) {
        return LULU_XPC_TIMEOUT;
    }
    if (reply == NULL || xpc_get_type(reply) != XPC_TYPE_DICTIONARY) {
        if (reply != NULL) {
            xpc_release(reply);
        }
        return LULU_XPC_CONNECTION_FAILED;
    }
    int64_t remote_result = xpc_dictionary_get_int64(reply, "result");
    if (remote_result != LULU_XPC_OK) {
        xpc_release(reply);
        return (int)remote_result;
    }
    const char *payload = xpc_dictionary_get_string(reply, "payload");
    if (payload == NULL || strnlen(payload, response_capacity) >= response_capacity) {
        xpc_release(reply);
        return LULU_XPC_PROTOCOL_ERROR;
    }
    int peer_result = copy_peer_facts(client->connection, server_peer);
    if (peer_result != LULU_XPC_OK) {
        xpc_release(reply);
        return peer_result;
    }
    strcpy(response_json, payload);
    xpc_release(reply);
    return LULU_XPC_OK;
}

void lulu_xpc_client_close(void *client_handle) {
    if (client_handle == NULL) {
        return;
    }
    lulu_xpc_client_v1 *client = client_handle;
    if (client->connection != NULL) {
        xpc_connection_cancel(client->connection);
        xpc_release(client->connection);
    }
    free(client);
}

static void accept_peer_connection(xpc_connection_t peer) {
    xpc_connection_set_event_handler(peer, ^(xpc_object_t event) {
        if (xpc_get_type(event) != XPC_TYPE_DICTIONARY) {
            return;
        }
        xpc_object_t reply = xpc_dictionary_create_reply(event);
        if (reply == NULL) {
            return;
        }
        lulu_xpc_peer_facts_v1 facts;
        const size_t response_capacity = 4 * 1024 * 1024;
        char *response = calloc(response_capacity, 1);
        const char *payload = xpc_dictionary_get_string(event, "payload");
        int result = payload == NULL || response == NULL || strnlen(payload, response_capacity) >= response_capacity
            ? LULU_XPC_PROTOCOL_ERROR
            : copy_peer_facts(peer, &facts);
        if (result == LULU_XPC_OK) {
            result = server_handler(
                payload,
                &facts,
                response,
                response_capacity,
                server_context
            );
        }
        xpc_dictionary_set_int64(reply, "result", result);
        xpc_dictionary_set_string(
            reply,
            "payload",
            result == LULU_XPC_OK ? response : "{\"ok\":false}"
        );
        xpc_connection_send_message(peer, reply);
        xpc_release(reply);
        free(response);
    });
    xpc_connection_activate(peer);
}

int lulu_xpc_server_run(
    const char *service_name,
    lulu_xpc_request_handler_v1 handler,
    void *context
) {
    if (
        service_name == NULL || service_name[0] == '\0' || handler == NULL ||
        !lulu_xpc_runtime_available()
    ) {
        return LULU_XPC_INVALID_ARGUMENT;
    }
    server_handler = handler;
    server_context = context;
    xpc_connection_t listener = xpc_connection_create_mach_service(
        service_name,
        NULL,
        XPC_CONNECTION_MACH_SERVICE_LISTENER
    );
    if (listener == NULL) {
        return LULU_XPC_CONNECTION_FAILED;
    }
    xpc_connection_set_event_handler(listener, ^(xpc_object_t event) {
        if (xpc_get_type(event) == XPC_TYPE_CONNECTION) {
            accept_peer_connection((xpc_connection_t)event);
        }
    });
    xpc_connection_activate(listener);
    dispatch_main();
    return LULU_XPC_HANDLER_ERROR;
}
