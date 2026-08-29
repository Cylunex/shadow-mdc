from enum import StrEnum


class QueryMode(StrEnum):
    CODE = "code"
    TEXT = "text"
    URL = "url"
    FINGERPRINT = "fingerprint"
    EXTERNAL_ID = "external_id"
    CATALOGUE = "catalogue"


class ContentFamily(StrEnum):
    JAV = "jav"
    CHINESE = "chinese"
    WESTERN = "western"
    ANIMATION = "animation"
    UNKNOWN = "unknown"


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


class MatchDecision(StrEnum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


class OperationKind(StrEnum):
    MOVE = "move"
    COPY = "copy"
    HARDLINK = "hardlink"
    WRITE_NFO = "write_nfo"


class ProviderRequirement(StrEnum):
    API_TOKEN = "api_token"
    COOKIE = "cookie"
    BROWSER = "browser"
    MANUAL_INTERACTION = "manual_interaction"
