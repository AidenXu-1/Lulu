#include "lulu_transport_identity.h"

#include <CommonCrypto/CommonDigest.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

static const uint8_t GOLDEN_CLIENT_DR[] = {
    0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
    0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,0x10
};
static const uint8_t GOLDEN_SERVER_DR[] = {
    0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,
    0x18,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f
};
static const uint8_t GOLDEN_CLIENT_CDHASH[] = {
    0x00,0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,0x99,
    0xaa,0xbb,0xcc,0xdd,0xee,0xff,0x00,0x11,0x22,0x33
};
static const uint8_t GOLDEN_SERVER_CDHASH[] = {
    0xff,0xee,0xdd,0xcc,0xbb,0xaa,0x99,0x88,0x77,0x66,
    0x55,0x44,0x33,0x22,0x11,0x00,0xff,0xee,0xdd,0xcc
};
static const uint8_t GOLDEN_CLIENT_DR_SHA256[32] = {
    0x5d,0xfb,0xab,0xee,0xdf,0x31,0x8b,0xf3,0x3c,0x09,0x27,0xc4,0x3d,0x76,0x30,0xf5,
    0x1b,0x82,0xf3,0x51,0x74,0x03,0x01,0x35,0x4f,0xa3,0xd7,0xfc,0x51,0xf0,0x13,0x2e
};
static const uint8_t GOLDEN_SERVER_DR_SHA256[32] = {
    0xfc,0x2e,0x2c,0x73,0x07,0x2b,0xfa,0x2b,0xda,0x03,0xff,0x93,0x07,0x47,0x2d,0xeb,
    0xd3,0xcc,0x81,0x05,0x02,0x8a,0x8a,0x9e,0x23,0x5e,0x35,0xba,0x8d,0x2e,0x37,0xf4
};
static const uint8_t GOLDEN_CLIENT_DIGEST[32] = {
    0x28,0x0f,0x85,0xa1,0x29,0x54,0xca,0x45,0x54,0x79,0xd7,0x95,0xcf,0x57,0x8b,0x57,
    0x40,0x63,0xe0,0x17,0x0f,0xbf,0x61,0x7c,0xca,0xe8,0x49,0xae,0xb7,0xa7,0x87,0xe7
};
static const uint8_t GOLDEN_SERVER_DIGEST[32] = {
    0x02,0x2d,0xc8,0xa8,0xc1,0xab,0x13,0x07,0x9e,0x0f,0x3c,0xfd,0xf7,0xcd,0xf4,0x6e,
    0x4a,0x55,0x73,0xc1,0xa8,0x14,0x95,0x48,0xbf,0x5d,0xaa,0xe5,0x6e,0xb3,0xe3,0xb6
};
static const uint8_t GOLDEN_TRANSCRIPT_DIGEST[32] = {
    0x14,0xaa,0x48,0x2f,0x15,0x40,0xc0,0x91,0x7f,0xb2,0xd9,0x67,0x09,0xf1,0xc2,0x77,
    0xa4,0xf7,0xa7,0xb5,0xe1,0x73,0x97,0x53,0x7b,0x6b,0xf9,0x29,0x10,0x9d,0xa3,0xe1
};

static lulu_byte_span span_from_c_string(const char *value) {
    lulu_byte_span span;
    span.bytes = (const uint8_t *)value;
    span.length = strlen(value);
    return span;
}

static bool valid_span(lulu_byte_span span, bool text) {
    if (span.bytes == NULL || span.length == 0 || span.length > UINT16_MAX) {
        return false;
    }
    return !text || memchr(span.bytes, 0, span.length) == NULL;
}

static bool valid_team_id(lulu_byte_span span) {
    if (span.length == 0) {
        return true;
    }
    return valid_span(span, true);
}

static bool has_suffix(lulu_byte_span value, const char *suffix) {
    size_t suffix_length = strlen(suffix);
    return value.length >= suffix_length &&
        memcmp(value.bytes + value.length - suffix_length, suffix, suffix_length) == 0;
}

static bool role_matches(uint8_t role, lulu_component_slot slot, lulu_byte_span identifier) {
    if (slot == LULU_COMPONENT_SLOT_CLIENT) {
        if (role == LULU_ROLE_CLI_CLIENT) {
            return has_suffix(identifier, ".cli");
        }
        if (role == LULU_ROLE_GUI_CLIENT) {
            return has_suffix(identifier, ".gui");
        }
        return false;
    }
    if (slot == LULU_COMPONENT_SLOT_SERVER) {
        return role == LULU_ROLE_CORE_SERVER && has_suffix(identifier, ".core");
    }
    return false;
}

static void put_u16be(uint8_t *destination, uint16_t value) {
    destination[0] = (uint8_t)(value >> 8);
    destination[1] = (uint8_t)value;
}

static void put_u64be(uint8_t *destination, uint64_t value) {
    for (size_t index = 0; index < 8; index++) {
        destination[index] = (uint8_t)(value >> (56u - (8u * index)));
    }
}

static void append_u16_span(uint8_t *destination, size_t *offset, lulu_byte_span span) {
    put_u16be(destination + *offset, (uint16_t)span.length);
    *offset += 2;
    if (span.length > 0) {
        memcpy(destination + *offset, span.bytes, span.length);
        *offset += span.length;
    }
}

static void sha256_bytes(const uint8_t *bytes, size_t length, uint8_t output[32]) {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    (void)CC_SHA256(bytes, (CC_LONG)length, output);
#pragma clang diagnostic pop
}

lulu_identity_status lulu_build_identity(
    lulu_component_slot slot,
    uint8_t role_u8,
    lulu_byte_span team_id,
    lulu_byte_span identifier,
    lulu_byte_span generation,
    lulu_byte_span cdhash,
    lulu_byte_span designated_requirement_blob,
    lulu_identity_result *result
) {
    if (result == NULL || !valid_team_id(team_id) || !valid_span(identifier, true) ||
        !valid_span(generation, true) || !valid_span(cdhash, false) ||
        !valid_span(designated_requirement_blob, false)) {
        return LULU_CANONICAL_INPUT_INVALID;
    }
    if (!role_matches(role_u8, slot, identifier)) {
        return LULU_IDENTITY_ROLE_INVALID;
    }

    size_t total = 1u + 8u + 32u;
    const size_t lengths[] = {team_id.length, identifier.length, generation.length, cdhash.length};
    for (size_t index = 0; index < 4; index++) {
        if (lengths[index] > SIZE_MAX - total) {
            return LULU_CANONICAL_INPUT_INVALID;
        }
        total += lengths[index];
    }
    if (total > LULU_IDENTITY_INPUT_CAPACITY) {
        return LULU_CANONICAL_INPUT_INVALID;
    }

    lulu_identity_result temporary;
    memset(&temporary, 0, sizeof(temporary));
    sha256_bytes(designated_requirement_blob.bytes, designated_requirement_blob.length,
                 temporary.designated_requirement_sha256);
    size_t offset = 0;
    temporary.identity_input[offset++] = role_u8;
    append_u16_span(temporary.identity_input, &offset, team_id);
    append_u16_span(temporary.identity_input, &offset, identifier);
    append_u16_span(temporary.identity_input, &offset, generation);
    append_u16_span(temporary.identity_input, &offset, cdhash);
    memcpy(temporary.identity_input + offset, temporary.designated_requirement_sha256, 32);
    offset += 32;
    temporary.identity_input_length = offset;
    sha256_bytes(temporary.identity_input, temporary.identity_input_length,
                 temporary.identity_digest);
    memcpy(result, &temporary, sizeof(temporary));
    return LULU_IDENTITY_OK;
}

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
) {
    if (result == NULL || selected_protocol == 0 || selected_protocol > UINT16_MAX ||
        client_nonce.bytes == NULL || client_nonce.length != 32 ||
        server_nonce.bytes == NULL || server_nonce.length != 32 ||
        client_identity_digest.bytes == NULL || client_identity_digest.length != 32 ||
        server_identity_digest.bytes == NULL || server_identity_digest.length != 32 ||
        !valid_span(client_generation, true) || !valid_span(server_generation, true) ||
        !valid_span(service_name, true)) {
        return LULU_CANONICAL_INPUT_INVALID;
    }

    const size_t magic_length = 11;
    size_t total = magic_length + 8u + 128u + 6u;
    const size_t lengths[] = {client_generation.length, server_generation.length, service_name.length};
    for (size_t index = 0; index < 3; index++) {
        if (lengths[index] > SIZE_MAX - total) {
            return LULU_CANONICAL_INPUT_INVALID;
        }
        total += lengths[index];
    }
    if (total > LULU_TRANSCRIPT_CAPACITY) {
        return LULU_CANONICAL_INPUT_INVALID;
    }

    lulu_transcript_result temporary;
    memset(&temporary, 0, sizeof(temporary));
    size_t offset = 0;
    memcpy(temporary.transcript + offset, "LULU-HS-V1", 10);
    offset += 10;
    temporary.transcript[offset++] = 0;
    put_u64be(temporary.transcript + offset, selected_protocol);
    offset += 8;
    memcpy(temporary.transcript + offset, client_nonce.bytes, 32); offset += 32;
    memcpy(temporary.transcript + offset, server_nonce.bytes, 32); offset += 32;
    memcpy(temporary.transcript + offset, client_identity_digest.bytes, 32); offset += 32;
    memcpy(temporary.transcript + offset, server_identity_digest.bytes, 32); offset += 32;
    append_u16_span(temporary.transcript, &offset, client_generation);
    append_u16_span(temporary.transcript, &offset, server_generation);
    append_u16_span(temporary.transcript, &offset, service_name);
    temporary.transcript_length = offset;
    sha256_bytes(temporary.transcript, temporary.transcript_length, temporary.transcript_sha256);
    memcpy(result, &temporary, sizeof(temporary));
    return LULU_IDENTITY_OK;
}

lulu_identity_status lulu_s1c3_golden_self_test(
    const uint8_t expected_client_identity_digest[32],
    const uint8_t expected_server_identity_digest[32],
    const uint8_t expected_transcript_digest[32],
    bool *transport_guard
) {
    if (transport_guard != NULL) {
        *transport_guard = false;
    }
    if (transport_guard == NULL || expected_client_identity_digest == NULL ||
        expected_server_identity_digest == NULL || expected_transcript_digest == NULL) {
        return LULU_CANONICAL_INPUT_INVALID;
    }

    const uint8_t client_nonce[32] = {
        0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,
        0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,0x18,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f
    };
    const uint8_t server_nonce[32] = {
        0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x29,0x2a,0x2b,0x2c,0x2d,0x2e,0x2f,
        0x30,0x31,0x32,0x33,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x3b,0x3c,0x3d,0x3e,0x3f
    };
    lulu_identity_result client;
    lulu_identity_result server;
    lulu_transcript_result transcript;
    lulu_identity_status status = lulu_build_identity(
        LULU_COMPONENT_SLOT_CLIENT, LULU_ROLE_CLI_CLIENT,
        span_from_c_string("ABCDE12345"),
        span_from_c_string("com.lulu.transcribe.trusted-test.cli"),
        span_from_c_string("S1C3-GOLDEN-CLI"),
        (lulu_byte_span){GOLDEN_CLIENT_CDHASH, sizeof(GOLDEN_CLIENT_CDHASH)},
        (lulu_byte_span){GOLDEN_CLIENT_DR, sizeof(GOLDEN_CLIENT_DR)}, &client);
    if (status != LULU_IDENTITY_OK) return status;
    status = lulu_build_identity(
        LULU_COMPONENT_SLOT_SERVER, LULU_ROLE_CORE_SERVER,
        span_from_c_string("ABCDE12345"),
        span_from_c_string("com.lulu.transcribe.trusted-test.core"),
        span_from_c_string("S1C3-GOLDEN-CORE"),
        (lulu_byte_span){GOLDEN_SERVER_CDHASH, sizeof(GOLDEN_SERVER_CDHASH)},
        (lulu_byte_span){GOLDEN_SERVER_DR, sizeof(GOLDEN_SERVER_DR)}, &server);
    if (status != LULU_IDENTITY_OK) return status;
    status = lulu_build_canonical_transcript(
        1, (lulu_byte_span){client_nonce, 32}, (lulu_byte_span){server_nonce, 32},
        (lulu_byte_span){client.identity_digest, 32},
        (lulu_byte_span){server.identity_digest, 32},
        span_from_c_string("S1C3-GOLDEN-CLI"), span_from_c_string("S1C3-GOLDEN-CORE"),
        span_from_c_string("com.lulu.transcribe.trusted-test.agent-core"), &transcript);
    if (status != LULU_IDENTITY_OK) return status;

    if (client.identity_input_length != 122 || server.identity_input_length != 124 ||
        transcript.transcript_length != 227 ||
        memcmp(client.designated_requirement_sha256, GOLDEN_CLIENT_DR_SHA256, 32) != 0 ||
        memcmp(server.designated_requirement_sha256, GOLDEN_SERVER_DR_SHA256, 32) != 0 ||
        memcmp(client.identity_digest, GOLDEN_CLIENT_DIGEST, 32) != 0 ||
        memcmp(server.identity_digest, GOLDEN_SERVER_DIGEST, 32) != 0 ||
        memcmp(transcript.transcript_sha256, GOLDEN_TRANSCRIPT_DIGEST, 32) != 0 ||
        memcmp(client.identity_digest, expected_client_identity_digest, 32) != 0 ||
        memcmp(server.identity_digest, expected_server_identity_digest, 32) != 0 ||
        memcmp(transcript.transcript_sha256, expected_transcript_digest, 32) != 0) {
        return LULU_GOLDEN_VECTOR_MISMATCH;
    }
    *transport_guard = true;
    return LULU_IDENTITY_OK;
}

const char *lulu_identity_status_oracle(lulu_identity_status status) {
    switch (status) {
        case LULU_IDENTITY_OK: return "OK";
        case LULU_IDENTITY_ROLE_INVALID: return "PEER_IDENTITY_ROLE_INVALID@identity_digest";
        case LULU_CANONICAL_INPUT_INVALID: return "HANDSHAKE_TRANSCRIPT_INVALID@canonical_input";
        case LULU_GOLDEN_VECTOR_MISMATCH: return "HANDSHAKE_GOLDEN_VECTOR_MISMATCH";
    }
    return "HANDSHAKE_TRANSCRIPT_INVALID@canonical_input";
}
