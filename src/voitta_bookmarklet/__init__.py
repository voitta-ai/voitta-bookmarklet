# Briefcase shim package — exposes the resources/ subtree.
# Business logic lives in backend/app/; this package is purely
# the bundling namespace for briefcase.

try:
    from voitta_bookmarklet._version import __version__  # noqa: F401
except Exception:
    __version__ = "unknown"
