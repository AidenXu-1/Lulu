-- Migration: 0002_device_self_deactivation
-- Purpose: dynamic per-license device guards and atomic current-device
-- self-deactivation recovery.
-- Forbidden: plaintext request IDs, license codes, lease tokens, device hashes,
-- raw hardware IDs, user content, and Cloudflare secrets.

CREATE TABLE IF NOT EXISTS device_deactivation_operations (
  id TEXT PRIMARY KEY,
  license_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  request_key_hash TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
  audit_event_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  committed_at TEXT,
  UNIQUE(license_id, device_id, request_key_hash),
  FOREIGN KEY(license_id) REFERENCES licenses(id),
  FOREIGN KEY(device_id) REFERENCES devices(id)
);

CREATE INDEX IF NOT EXISTS idx_device_deactivation_license_device
  ON device_deactivation_operations(license_id, device_id, created_at);

CREATE TRIGGER IF NOT EXISTS licenses_max_devices_insert_guard
BEFORE INSERT ON licenses
WHEN NEW.max_devices IS NULL
  OR NEW.max_devices < 1
  OR NEW.max_devices <> CAST(NEW.max_devices AS INTEGER)
BEGIN
  SELECT RAISE(ABORT, 'invalid_max_devices');
END;

CREATE TRIGGER IF NOT EXISTS licenses_max_devices_update_guard
BEFORE UPDATE OF max_devices ON licenses
WHEN NEW.max_devices IS NULL
  OR NEW.max_devices < 1
  OR NEW.max_devices <> CAST(NEW.max_devices AS INTEGER)
BEGIN
  SELECT RAISE(ABORT, 'invalid_max_devices');
END;

CREATE TRIGGER IF NOT EXISTS devices_dynamic_limit_insert_guard
BEFORE INSERT ON devices
WHEN NEW.status = 'active'
BEGIN
  SELECT CASE
    WHEN NOT EXISTS (
      SELECT 1
      FROM licenses
      WHERE id = NEW.license_id AND status = 'active'
    )
    THEN RAISE(ABORT, 'license_unavailable')
    WHEN (
      SELECT max_devices IS NULL
        OR max_devices < 1
        OR max_devices <> CAST(max_devices AS INTEGER)
      FROM licenses
      WHERE id = NEW.license_id
    )
    THEN RAISE(ABORT, 'invalid_max_devices')
    WHEN (
      SELECT COUNT(*)
      FROM devices
      WHERE license_id = NEW.license_id AND status = 'active'
    ) >= (
      SELECT max_devices
      FROM licenses
      WHERE id = NEW.license_id
    )
    THEN RAISE(ABORT, 'device_limit')
  END;
END;

CREATE TRIGGER IF NOT EXISTS devices_dynamic_limit_reactivate_guard
BEFORE UPDATE OF status, license_id ON devices
WHEN NEW.status = 'active'
  AND (OLD.status <> 'active' OR OLD.license_id <> NEW.license_id)
BEGIN
  SELECT CASE
    WHEN NOT EXISTS (
      SELECT 1
      FROM licenses
      WHERE id = NEW.license_id AND status = 'active'
    )
    THEN RAISE(ABORT, 'license_unavailable')
    WHEN (
      SELECT max_devices IS NULL
        OR max_devices < 1
        OR max_devices <> CAST(max_devices AS INTEGER)
      FROM licenses
      WHERE id = NEW.license_id
    )
    THEN RAISE(ABORT, 'invalid_max_devices')
    WHEN (
      SELECT COUNT(*)
      FROM devices
      WHERE license_id = NEW.license_id
        AND status = 'active'
        AND id <> NEW.id
    ) >= (
      SELECT max_devices
      FROM licenses
      WHERE id = NEW.license_id
    )
    THEN RAISE(ABORT, 'device_limit')
  END;
END;

CREATE TRIGGER IF NOT EXISTS device_deactivation_commit_guard
BEFORE UPDATE OF status ON device_deactivation_operations
WHEN NEW.status = 'committed'
BEGIN
  SELECT CASE
    WHEN OLD.status <> 'pending'
    THEN RAISE(ABORT, 'deactivation_operation_state')
    WHEN NOT EXISTS (
      SELECT 1
      FROM devices
      WHERE id = NEW.device_id
        AND license_id = NEW.license_id
        AND status = 'revoked'
    )
    THEN RAISE(ABORT, 'deactivation_device_not_revoked')
    WHEN EXISTS (
      SELECT 1
      FROM lease_tokens
      WHERE license_id = NEW.license_id
        AND device_id = NEW.device_id
        AND status = 'active'
    )
    THEN RAISE(ABORT, 'deactivation_lease_still_active')
    WHEN NOT EXISTS (
      SELECT 1
      FROM audit_events
      WHERE id = NEW.audit_event_id
        AND license_id = NEW.license_id
        AND device_id = NEW.device_id
        AND event_type = 'self_deactivate_committed'
    )
    THEN RAISE(ABORT, 'deactivation_audit_missing')
  END;
END;
