"""Domain errors. In models so any layer may catch them without an upward import."""


class UnknownTableError(ValueError):
    """A caller targeted a table outside the edit/delete allow-list."""


class UneditableFieldError(ValueError):
    """A caller tried to set a column outside the edit allow-list."""


class ReferenceDataMissingError(RuntimeError):
    """Growth reference tables are absent or unusable.

    Raised instead of returning an approximation: a wrong centile is worse
    than an unavailable one (SPEC 1.1).
    """
