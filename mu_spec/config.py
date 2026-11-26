"""The configuration names in the start check's order, as views of mu_spec.outside's one table.

Derived constants only: reads no environment, no file and no clock, builds no record, defines no
loader. Every value is computed from mu_spec.outside at import; the only literals here are the six
Config field names the start check enumerates, in its order, and the override's key.
"""

# EMPTY_DIR and PROJECTS_SUBDIR are re-exported as they are
from mu_spec.outside import ABSENT_DEFAULTS, EMPTY_DIR, ENV_NAMES, PROJECTS_SUBDIR  # noqa: F401

# the six values the node injects, in the start check's order
VALUE_FIELDS = ("state_dir", "host", "port", "prompts_dir", "git_sha", "github_token")
VALUE_NAMES = tuple(ENV_NAMES[f] for f in VALUE_FIELDS)
# the projects-location override; the node never sets it
ROOT_NAME = ENV_NAMES["mu_spec_root"]
ALL_NAMES = VALUE_NAMES + (ROOT_NAME,)
# the defaults for a missing value, keyed by variable name, in the outside table's order
MISSING_DEFAULTS = {ENV_NAMES[f]: v for f, v in ABSENT_DEFAULTS.items()}
