from pathlib import Path

CANONICAL_RELEASE_REPOSITORY = "AidenXu-1/Lulu"
LEGACY_RELEASE_REPOSITORY = "AidenXu-1/lulu-releases"
ALLOWED_RELEASE_REPOSITORIES = frozenset(
    {CANONICAL_RELEASE_REPOSITORY, LEGACY_RELEASE_REPOSITORY}
)
ALLOWED_GITHUB_RELEASE_PREFIXES = tuple(
    f"/{repository}/releases/" for repository in sorted(ALLOWED_RELEASE_REPOSITORIES)
)
MANIFEST_URL = f"https://github.com/{CANONICAL_RELEASE_REPOSITORY}/releases/latest/download/version.json"
SIGNATURE_URL = f"https://github.com/{CANONICAL_RELEASE_REPOSITORY}/releases/latest/download/version.json.sig"
RELEASES_URL = f"https://github.com/{CANONICAL_RELEASE_REPOSITORY}/releases"
ALLOWED_INITIAL_HOSTS = frozenset({"github.com"})
ALLOWED_REDIRECT_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 5
READ_IDLE_TIMEOUT_SECONDS = 15
MANIFEST_TOTAL_TIMEOUT_SECONDS = 20
SIG_TOTAL_TIMEOUT_SECONDS = 20
ZIP_TOTAL_TIMEOUT_SECONDS = 1800
MAX_MANIFEST_BYTES = 131072
MAX_SIG_BASE64_BYTES = 16384
MAX_ZIP_BYTES = 3221225472
MAX_URL_BYTES = 2048
MAX_USER_AGENT_BYTES = 96
MAX_ZIP_ENTRIES = 20000
MAX_SINGLE_UNCOMPRESSED_BYTES = 1073741824
MAX_TOTAL_UNCOMPRESSED_BYTES = 4294967296
MAX_COMPRESSION_RATIO = 100
MAX_ZIP_PATH_BYTES = 512
MAX_ZIP_PATH_DEPTH = 32
MAX_SYMLINK_DEPTH = 8
KEY_ID = "lulu-update-rsa3072-2026-07"
CHECK_INTERVAL_SECONDS = 86400
# Earlier production clients launch an installer with a normal-exit race.
# This semantic version boundary also works in their existing signed manifest.
MINIMUM_AUTO_UPDATE_VERSION = "4.1.6"
MANUAL_DOWNLOAD_URL = f"https://github.com/{CANONICAL_RELEASE_REPOSITORY}/releases/latest"
# Only helpers advertising this flag may use the longer GUI preparation budget.
# Already-installed helpers still roll back after 45 seconds.
AUTHORIZATION_WAIT_FLAG = "--lulu-update-authorization-wait"
PREPARATION_TIMEOUT_SECONDS = 300
UPDATE_READY_TIMEOUT_SECONDS = PREPARATION_TIMEOUT_SECONDS + 60


def default_updates_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "Lulu" / "updates"
