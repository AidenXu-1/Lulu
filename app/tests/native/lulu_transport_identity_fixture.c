#include "lulu_transport_identity.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const uint8_t CLIENT_NONCE[32] = {
    0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,
    0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,0x18,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f
};
static const uint8_t SERVER_NONCE[32] = {
    0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x29,0x2a,0x2b,0x2c,0x2d,0x2e,0x2f,
    0x30,0x31,0x32,0x33,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x3b,0x3c,0x3d,0x3e,0x3f
};
static const uint8_t CLIENT_CDHASH[20] = {
    0x00,0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,0x99,
    0xaa,0xbb,0xcc,0xdd,0xee,0xff,0x00,0x11,0x22,0x33
};
static const uint8_t SERVER_CDHASH[20] = {
    0xff,0xee,0xdd,0xcc,0xbb,0xaa,0x99,0x88,0x77,0x66,
    0x55,0x44,0x33,0x22,0x11,0x00,0xff,0xee,0xdd,0xcc
};
static const uint8_t CLIENT_DR[16] = {
    0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
    0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,0x10
};
static const uint8_t SERVER_DR[16] = {
    0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,
    0x18,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f
};
static const uint8_t CLIENT_DIGEST[32] = {
    0x28,0x0f,0x85,0xa1,0x29,0x54,0xca,0x45,0x54,0x79,0xd7,0x95,0xcf,0x57,0x8b,0x57,
    0x40,0x63,0xe0,0x17,0x0f,0xbf,0x61,0x7c,0xca,0xe8,0x49,0xae,0xb7,0xa7,0x87,0xe7
};
static const uint8_t SERVER_DIGEST[32] = {
    0x02,0x2d,0xc8,0xa8,0xc1,0xab,0x13,0x07,0x9e,0x0f,0x3c,0xfd,0xf7,0xcd,0xf4,0x6e,
    0x4a,0x55,0x73,0xc1,0xa8,0x14,0x95,0x48,0xbf,0x5d,0xaa,0xe5,0x6e,0xb3,0xe3,0xb6
};
static const uint8_t TRANSCRIPT_DIGEST[32] = {
    0x14,0xaa,0x48,0x2f,0x15,0x40,0xc0,0x91,0x7f,0xb2,0xd9,0x67,0x09,0xf1,0xc2,0x77,
    0xa4,0xf7,0xa7,0xb5,0xe1,0x73,0x97,0x53,0x7b,0x6b,0xf9,0x29,0x10,0x9d,0xa3,0xe1
};

static lulu_byte_span text_span(const char *value) {
    return (lulu_byte_span){(const uint8_t *)value, strlen(value)};
}

static void print_hex(const uint8_t *bytes, size_t length) {
    for (size_t index = 0; index < length; index++) {
        printf("%02x", bytes[index]);
    }
}

static bool build_golden(
    lulu_identity_result *client,
    lulu_identity_result *server,
    lulu_transcript_result *transcript
) {
    if (lulu_build_identity(
            LULU_COMPONENT_SLOT_CLIENT, LULU_ROLE_CLI_CLIENT,
            text_span("ABCDE12345"), text_span("com.lulu.transcribe.trusted-test.cli"),
            text_span("S1C3-GOLDEN-CLI"), (lulu_byte_span){CLIENT_CDHASH, 20},
            (lulu_byte_span){CLIENT_DR, 16}, client) != LULU_IDENTITY_OK) return false;
    if (lulu_build_identity(
            LULU_COMPONENT_SLOT_SERVER, LULU_ROLE_CORE_SERVER,
            text_span("ABCDE12345"), text_span("com.lulu.transcribe.trusted-test.core"),
            text_span("S1C3-GOLDEN-CORE"), (lulu_byte_span){SERVER_CDHASH, 20},
            (lulu_byte_span){SERVER_DR, 16}, server) != LULU_IDENTITY_OK) return false;
    return lulu_build_canonical_transcript(
        1, (lulu_byte_span){CLIENT_NONCE, 32}, (lulu_byte_span){SERVER_NONCE, 32},
        (lulu_byte_span){client->identity_digest, 32},
        (lulu_byte_span){server->identity_digest, 32},
        text_span("S1C3-GOLDEN-CLI"), text_span("S1C3-GOLDEN-CORE"),
        text_span("com.lulu.transcribe.trusted-test.agent-core"), transcript) == LULU_IDENTITY_OK;
}

static int print_failure(const char *case_id, lulu_identity_status status, bool sentinel_unchanged) {
    printf("{\"case_id\":\"%s\",\"effect_count\":0,\"error\":\"%s\","
           "\"output_length\":0,\"sentinel_hex\":\"",
           case_id, lulu_identity_status_oracle(status));
    for (size_t index = 0; index < 16; index++) printf("a5");
    printf("\",\"sentinel_unchanged\":%s,\"status\":\"error\","
           "\"transport_guard\":false}", sentinel_unchanged ? "true" : "false");
    return sentinel_unchanged ? 0 : 4;
}

static int run_golden(void) {
    lulu_identity_result client;
    lulu_identity_result server;
    lulu_transcript_result transcript;
    bool guard = false;
    lulu_identity_status status = lulu_s1c3_golden_self_test(
        CLIENT_DIGEST, SERVER_DIGEST, TRANSCRIPT_DIGEST, &guard);
    if (status != LULU_IDENTITY_OK || !guard || !build_golden(&client, &server, &transcript)) {
        return print_failure("golden_full", LULU_GOLDEN_VECTOR_MISMATCH, true);
    }
    printf("{\"case_id\":\"golden_full\",\"client_designated_requirement_sha256\":\"");
    print_hex(client.designated_requirement_sha256, 32);
    printf("\",\"client_identity_digest\":\""); print_hex(client.identity_digest, 32);
    printf("\",\"client_identity_input_hex\":\""); print_hex(client.identity_input, client.identity_input_length);
    printf("\",\"client_identity_input_length\":%zu,\"effect_count\":0,"
           "\"server_designated_requirement_sha256\":\"", client.identity_input_length);
    print_hex(server.designated_requirement_sha256, 32);
    printf("\",\"server_identity_digest\":\""); print_hex(server.identity_digest, 32);
    printf("\",\"server_identity_input_hex\":\""); print_hex(server.identity_input, server.identity_input_length);
    printf("\",\"server_identity_input_length\":%zu,\"status\":\"ok\","
           "\"transcript_hex\":\"", server.identity_input_length);
    print_hex(transcript.transcript, transcript.transcript_length);
    printf("\",\"transcript_length\":%zu,\"transcript_sha256\":\"",
           transcript.transcript_length);
    print_hex(transcript.transcript_sha256, 32);
    printf("\",\"transport_guard\":true}");
    return 0;
}

static int run_ad_hoc_zero_team(void) {
    lulu_identity_result result;
    lulu_identity_status status = lulu_build_identity(
        LULU_COMPONENT_SLOT_CLIENT, LULU_ROLE_CLI_CLIENT,
        (lulu_byte_span){NULL, 0}, text_span("com.lulu.transcribe.trusted-test.cli"),
        text_span("S1C3-GOLDEN-CLI"), (lulu_byte_span){CLIENT_CDHASH, 20},
        (lulu_byte_span){CLIENT_DR, 16}, &result);
    if (status != LULU_IDENTITY_OK) return 10;
    printf("{\"case_id\":\"ad_hoc_zero_team\",\"effect_count\":0,"
           "\"identity_digest\":\"");
    print_hex(result.identity_digest, 32);
    printf("\",\"identity_input_hex\":\"");
    print_hex(result.identity_input, result.identity_input_length);
    printf("\",\"identity_input_length\":%zu,\"status\":\"ok\","
           "\"transport_guard\":false}", result.identity_input_length);
    return 0;
}

static int run_identity_failure(const char *case_id) {
    lulu_identity_result output;
    memset(&output, 0xa5, sizeof(output));
    output.identity_input_length = 0;
    lulu_identity_result before;
    memcpy(&before, &output, sizeof(output));
    uint8_t byte = 0x01;
    uint8_t nul_generation[] = {'B','A','D',0,'X'};
    lulu_component_slot slot = LULU_COMPONENT_SLOT_CLIENT;
    uint8_t role = LULU_ROLE_CLI_CLIENT;
    lulu_byte_span team = text_span("ABCDE12345");
    lulu_byte_span identifier = text_span("com.lulu.transcribe.trusted-test.cli");
    lulu_byte_span generation = text_span("S1C3-GOLDEN-CLI");
    lulu_byte_span cdhash = (lulu_byte_span){CLIENT_CDHASH, 20};
    lulu_byte_span dr = (lulu_byte_span){CLIENT_DR, 16};
    lulu_identity_status expected = LULU_IDENTITY_ROLE_INVALID;

    if (strcmp(case_id, "identity_client_with_core_role") == 0) role = LULU_ROLE_CORE_SERVER;
    else if (strcmp(case_id, "identity_server_with_cli_role") == 0) {
        slot = LULU_COMPONENT_SLOT_SERVER;
        role = LULU_ROLE_CLI_CLIENT;
        identifier = text_span("com.lulu.transcribe.trusted-test.core");
        generation = text_span("S1C3-GOLDEN-CORE");
    } else if (strcmp(case_id, "identity_cli_identifier_with_gui_role") == 0) role = LULU_ROLE_GUI_CLIENT;
    else {
        expected = LULU_CANONICAL_INPUT_INVALID;
        if (strcmp(case_id, "malformed_dr_len_0") == 0) dr.length = 0;
        else if (strcmp(case_id, "malformed_dr_len_65536") == 0) dr = (lulu_byte_span){&byte, 65536};
        else if (strcmp(case_id, "malformed_nonzero_null") == 0) team = (lulu_byte_span){NULL, 1};
        else if (strcmp(case_id, "malformed_generation_nul") == 0) generation = (lulu_byte_span){nul_generation, sizeof(nul_generation)};
        else if (strcmp(case_id, "malformed_total_overflow") == 0) team = (lulu_byte_span){&byte, SIZE_MAX};
        else return 3;
    }
    lulu_identity_status status = lulu_build_identity(slot, role, team, identifier, generation, cdhash, dr, &output);
    if (status != expected) return 5;
    return print_failure(case_id, status, memcmp(&before, &output, sizeof(output)) == 0);
}

static int run_nonce_failure(const char *case_id, size_t nonce_length) {
    lulu_identity_result client;
    lulu_identity_result server;
    lulu_transcript_result valid;
    if (!build_golden(&client, &server, &valid)) return 6;
    lulu_transcript_result output;
    memset(&output, 0xa5, sizeof(output));
    output.transcript_length = 0;
    lulu_transcript_result before;
    memcpy(&before, &output, sizeof(output));
    lulu_identity_status status = lulu_build_canonical_transcript(
        1, (lulu_byte_span){CLIENT_NONCE, nonce_length}, (lulu_byte_span){SERVER_NONCE, 32},
        (lulu_byte_span){client.identity_digest, 32}, (lulu_byte_span){server.identity_digest, 32},
        text_span("S1C3-GOLDEN-CLI"), text_span("S1C3-GOLDEN-CORE"),
        text_span("com.lulu.transcribe.trusted-test.agent-core"), &output);
    if (status != LULU_CANONICAL_INPUT_INVALID) return 7;
    return print_failure(case_id, status, memcmp(&before, &output, sizeof(output)) == 0);
}

static int run_golden_mismatch(const char *case_id) {
    uint8_t client[32], server[32], transcript[32];
    memcpy(client, CLIENT_DIGEST, 32); memcpy(server, SERVER_DIGEST, 32);
    memcpy(transcript, TRANSCRIPT_DIGEST, 32);
    if (strcmp(case_id, "golden_client_digest_bitflip") == 0) client[0] ^= 1;
    else if (strcmp(case_id, "golden_server_digest_bitflip") == 0) server[0] ^= 1;
    else if (strcmp(case_id, "golden_transcript_digest_bitflip") == 0) transcript[0] ^= 1;
    else return 8;
    bool guard = true;
    lulu_identity_status status = lulu_s1c3_golden_self_test(client, server, transcript, &guard);
    if (status != LULU_GOLDEN_VECTOR_MISMATCH || guard) return 9;
    return print_failure(case_id, status, true);
}

static int run_null_digest(const char *case_id) {
    const uint8_t *client = CLIENT_DIGEST;
    const uint8_t *server = SERVER_DIGEST;
    const uint8_t *transcript = TRANSCRIPT_DIGEST;
    if (strcmp(case_id, "golden_null_client_digest") == 0) client = NULL;
    else if (strcmp(case_id, "golden_null_server_digest") == 0) server = NULL;
    else if (strcmp(case_id, "golden_null_transcript_digest") == 0) transcript = NULL;
    else return 11;
    bool guard = true;
    lulu_identity_status status = lulu_s1c3_golden_self_test(client, server, transcript, &guard);
    if (status != LULU_CANONICAL_INPUT_INVALID || guard) return 12;
    return print_failure(case_id, status, true);
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    const char *case_id = argv[1];
    if (strcmp(case_id, "golden_full") == 0) return run_golden();
    if (strcmp(case_id, "ad_hoc_zero_team") == 0) return run_ad_hoc_zero_team();
    if (strncmp(case_id, "golden_null_", 12) == 0) return run_null_digest(case_id);
    if (strncmp(case_id, "golden_", 7) == 0) return run_golden_mismatch(case_id);
    if (strcmp(case_id, "malformed_nonce_len_31") == 0) return run_nonce_failure(case_id, 31);
    if (strcmp(case_id, "malformed_nonce_len_33") == 0) return run_nonce_failure(case_id, 33);
    return run_identity_failure(case_id);
}
