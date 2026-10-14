-- Migration: 0001_initial
-- Purpose: ADR-0021 authorization metadata only.
-- Forbidden: plaintext license codes, plaintext lease tokens, user content,
-- model paths, third-party API keys, raw hardware IDs, Cloudflare secrets.

CREATE TABLE IF NOT EXISTS identities (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'anonymous',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS licenses (
  id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired', 'suspended')),
  max_devices INTEGER NOT NULL DEFAULT 2,
  expires_at TEXT,
  owner_identity_id TEXT,
  entitlements_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT,
  FOREIGN KEY(owner_identity_id) REFERENCES identities(id)
);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  identity_id TEXT NOT NULL,
  license_id TEXT NOT NULL,
  device_hash TEXT NOT NULL,
  device_label TEXT,
  platform TEXT,
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_app_version TEXT,
  UNIQUE(license_id, device_hash),
  FOREIGN KEY(identity_id) REFERENCES identities(id),
  FOREIGN KEY(license_id) REFERENCES licenses(id)
);

CREATE TABLE IF NOT EXISTS lease_tokens (
  id TEXT PRIMARY KEY,
  license_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT,
  revoke_reason TEXT,
  FOREIGN KEY(license_id) REFERENCES licenses(id),
  FOREIGN KEY(device_id) REFERENCES devices(id)
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  license_id TEXT,
  device_id TEXT,
  event_type TEXT NOT NULL,
  reason TEXT,
  app_version TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
  bucket TEXT PRIMARY KEY,
  attempts INTEGER NOT NULL DEFAULT 0,
  reset_at TEXT NOT NULL,
  cooldown_until TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_licenses_code_hash ON licenses(code_hash);
CREATE INDEX IF NOT EXISTS idx_licenses_status_created ON licenses(status, created_at);
CREATE INDEX IF NOT EXISTS idx_devices_license_status ON devices(license_id, status);
CREATE INDEX IF NOT EXISTS idx_devices_license_hash ON devices(license_id, device_hash);
CREATE INDEX IF NOT EXISTS idx_leases_license_device_status ON lease_tokens(license_id, device_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_license_created ON audit_events(license_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_rate_limits_cooldown ON rate_limits(cooldown_until);
