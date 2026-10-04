from __future__ import annotations


class BudgetError(RuntimeError):
    """Base class for local budget failures that never includes request data."""


class BudgetExceededError(BudgetError):
    pass


class BudgetInvariantError(BudgetError):
    pass


class BudgetUsageError(BudgetError):
    pass


class BudgetPriceWindowError(BudgetInvariantError):
    pass
