#ifndef LULU_TRANSPORT_IDENTITY_H
#define LULU_TRANSPORT_IDENTITY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LULU_SHA256_LENGTH 32u
#define LULU_IDENTITY_INPUT_CAPACITY 262181u
#define LULU_TRANSCRIPT_CAPACITY 196758u

typedef enum {
    LULU_COMPONENT_SLOT_CLIENT = 1,
    LULU_COMPONENT_SLOT_SERVER = 2
} lulu_component_slot;

typedef enum {
    LULU_ROLE_CLI_CLIENT = 0x01,
    LULU_ROLE_GUI_CLIENT = 0x02,
    LULU_ROLE_CORE_SERVER = 0x03
} lulu_component_role;

typedef enum {
    LULU_IDENTITY_OK = 0,
    LULU_IDENTITY_ROLE_INVALID = 1,
    LULU_CANONICAL_INPUT_INVALID = 2,
    LULU_GOLDEN_VECTOR_MISMATCH = 3
} lulu_identity_status;

typedef struct {
    const uint8_t *bytes;
    size_t length;
} lulu_byte_span;

typedef struct {
    uint8_t designated_requirement_sha256[LULU_SHA256_LENGTH];
    uint8_t identity_input[LULU_IDENTITY_INPUT_CAPACITY];
    size_t identity_input_length;
    uint8_t identity_digest[LULU_SHA256_LENGTH];
} lulu_identity_result;

typedef struct {
    uint8_t transcript[LULU_TRANSCRIPT_CAPACITY];
    size_t transcript_length;
    uint8_t transcript_sha256[LULU_SHA256_LENGTH];
} lulu_transcript_result;

lulu_identity_status lulu_build_identity(
    lulu_component_slot slot,
    uint8_t role_u8,
    /* Ad-hoc identity is encoded uniquely as team_id.length == 0; bytes may be NULL. */
    lulu_byte_span team_id,
    lulu_byte_span identifier,
    lulu_byte_span generation,
    lulu_byte_span cdhash,
    lulu_byte_span designated_requirement_blob,
    lulu_identity_result *result
);

lulu_identity_status lulu_build_canonical_transcript(
    uint64_t selected_protocol,
    lulu_byte_span client_nonce,
    lulu_byte_span server_nonce,
    lulu_byte_span client_identity_digest,
    lulu_byte_span server_identity_digest,
    lulu_byte_span client_generation,
    lulu_byte_span server_generation,
    lulu_byte_span service_name,
    lulu_transcript_result *result
);

lulu_identity_status lulu_s1c3_golden_self_test(
    const uint8_t expected_client_identity_digest[LULU_SHA256_LENGTH],
    const uint8_t expected_server_identity_digest[LULU_SHA256_LENGTH],
    const uint8_t expected_transcript_digest[LULU_SHA256_LENGTH],
    bool *transport_guard
);

const char *lulu_identity_status_oracle(lulu_identity_status status);

#endif
