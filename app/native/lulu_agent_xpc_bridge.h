#ifndef LULU_AGENT_XPC_BRIDGE_H
#define LULU_AGENT_XPC_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

#define LULU_XPC_PATH_MAX 4096
#define LULU_XPC_TOKEN_BYTES 32

typedef struct {
    uint32_t schema_version;
    int32_t pid;
    uint32_t effective_uid;
    uint32_t effective_gid;
    uint32_t audit_uid;
    uint32_t audit_session_id;
    int32_t pidversion;
    uint64_t device;
    uint64_t inode;
    uint64_t size;
    uint32_t mode;
    uint64_t connection_instance;
    uint8_t audit_token[LULU_XPC_TOKEN_BYTES];
    char executable_path[LULU_XPC_PATH_MAX];
} lulu_xpc_peer_facts_v1;

typedef int (*lulu_xpc_request_handler_v1)(
    const char *request_json,
    const lulu_xpc_peer_facts_v1 *peer,
    char *response_json,
    size_t response_capacity,
    void *context
);

int lulu_xpc_runtime_available(void);
uint32_t lulu_xpc_current_audit_session(void);
void *lulu_xpc_client_open(const char *service_name);
int lulu_xpc_client_exchange(
    void *client_handle,
    const char *request_json,
    char *response_json,
    size_t response_capacity,
    lulu_xpc_peer_facts_v1 *server_peer,
    uint32_t timeout_milliseconds
);
void lulu_xpc_client_close(void *client_handle);
int lulu_xpc_server_run(
    const char *service_name,
    lulu_xpc_request_handler_v1 handler,
    void *context
);

#endif
