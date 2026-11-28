"""gTR by Gruzin."""

BETA_NUMBER = 8
BETA_FIX = 0
BETA_VERSION = (BETA_NUMBER, BETA_FIX)


def beta_label(number, fix=0):
    suffix = f" (Fix {fix})" if fix > 1 else " (Fix)" if fix else ""
    return f"Beta {number}{suffix}"


def beta_folder(number, fix=0):
    suffix = f"-Fix-{fix}" if fix > 1 else "-Fix" if fix else ""
    return f"gTR-Beta-{number}{suffix}"


__version__ = beta_label(*BETA_VERSION)
RELEASE_NAME = beta_folder(*BETA_VERSION)
DISTRIBUTION_VERSION = f"{BETA_NUMBER}b{int(BETA_FIX)}"
