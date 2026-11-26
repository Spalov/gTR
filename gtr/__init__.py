"""gTR by Gruzin."""

BETA_NUMBER = 6
BETA_FIX = False
BETA_VERSION = (BETA_NUMBER, BETA_FIX)


def beta_label(number, fix=False):
    return f"Beta {number}" + (" (Fix)" if fix else "")


def beta_folder(number, fix=False):
    return f"gTR-Beta-{number}" + ("-Fix" if fix else "")


__version__ = beta_label(*BETA_VERSION)
RELEASE_NAME = beta_folder(*BETA_VERSION)
DISTRIBUTION_VERSION = f"{BETA_NUMBER}b{int(BETA_FIX)}"
