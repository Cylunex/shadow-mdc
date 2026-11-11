from enum import StrEnum


class QueryMode(StrEnum):
    CODE = "code"
    TEXT = "text"
    URL = "url"
    FINGERPRINT = "fingerprint"
    EXTERNAL_ID = "external_id"
    CATALOGUE = "catalogue"


class ContentFamily(StrEnum):
    """Legacy recognition/source families, not work types, languages or production regions."""
    JAV = "jav"
    CHINESE = "chinese"
    KOREAN = "korean"
    WESTERN = "western"
    ANIMATION = "animation"
    UNKNOWN = "unknown"


class MediaCategory(StrEnum):
    """Legacy library routing partitions; Europe includes the western source family."""
    JAPAN = "Japan"
    CHINA = "China"
    KOREA = "Korea"
    EUROPE = "Europe"
    OTHER = "Other"


class RecognitionScope(StrEnum):
    ALL = "all"
    JAV_ONLY = "jav_only"


class IdentityKind(StrEnum):
    CODE = "code"
    PROVIDER_ID = "provider_id"
    SOURCE_URL = "source_url"
    FINGERPRINT = "fingerprint"
    ALIAS = "alias"


class CandidateState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AssetState(StrEnum):
    NEW = "new"
    REVIEW = "review"
    IDENTIFIED = "identified"
    ERROR = "error"
    IGNORED = "ignored"


class TaskStatus(StrEnum):
    """Lifecycle of a general TaskRun; checkpoint phases and offline jobs use other domains."""

    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"

    @property
    def terminal(self) -> bool:
        return self in (
            TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED,
            TaskStatus.CANCELLED, TaskStatus.INTERRUPTED, TaskStatus.PAUSED,
        )


TERMINAL_TASK_STATUSES = frozenset(state for state in TaskStatus if state.terminal)


class OfflineStatus(StrEnum):
    """Local offline pipeline status, independent of catalogue/media presence."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @classmethod
    def from_legacy(cls, value: str) -> "OfflineStatus":
        if value == "completed":
            return cls.DONE
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class LibraryPresence(StrEnum):
    """Registration in the local catalogue; a completed remote job alone is not media."""

    ABSENT = "absent"
    CATALOG_ONLY = "catalog_only"
    IN_LIBRARY = "in_library"

    @classmethod
    def from_flags(cls, *, in_catalog: bool, has_media: bool) -> "LibraryPresence":
        if not in_catalog:
            return cls.ABSENT
        return cls.IN_LIBRARY if has_media else cls.CATALOG_ONLY


class MediaAvailability(StrEnum):
    """A work query's media bindings; wanted preference is a separate boolean filter."""

    ALL = "all"
    FILES = "files"
    EMBY = "emby"
    METADATA = "metadata"
    UNDOWNLOADED = "undownloaded"


class OrganizationKind(StrEnum):
    """Organization form; a series is a different entity, not a member of this enum."""

    ORGANIZATION = "organization"
    STUDIO = "studio"
    LABEL = "label"
    PLATFORM = "platform"


class OrganizationRole(StrEnum):
    """A particular work's credit, independent of its organization's form."""

    STUDIO = "studio"
    DISTRIBUTOR = "distributor"
    LABEL = "label"
    PLATFORM = "platform"
    CREDITED = "credited"


class MatchDecision(StrEnum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


class OperationKind(StrEnum):
    MOVE = "move"
    COPY = "copy"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    WRITE_NFO = "write_nfo"
    DELETE_FILTERED_FILE = "delete_filtered_file"
    REMOVE_DIRECTORY = "remove_directory"


class OutputMode(StrEnum):
    SIDECAR = "sidecar"
    COPY = "copy"
    MOVE = "move"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"


class NfoPolicy(StrEnum):
    ERROR = "error"
    SKIP = "skip"
    REPLACE = "replace"


class ProviderRequirement(StrEnum):
    API_TOKEN = "api_token"
    COOKIE = "cookie"
    BROWSER = "browser"
    MANUAL_INTERACTION = "manual_interaction"


class CollectionKind(StrEnum):
    """Legacy collection projection; series dossiers and organization credits are separate domains."""

    ORGANIZATION = "organization"
    SERIES = "series"
    STUDIO = "studio"
    PLATFORM = "platform"
    LABEL = "label"
