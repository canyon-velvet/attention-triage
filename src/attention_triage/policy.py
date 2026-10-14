"""policy.yaml (SPEC.md §5): which rules run, how severe their flags are, and how many days of
events are kept.

The file is created with the defaults when missing. A rule it leaves out keeps its defaults. Unknown
keys are rejected rather than ignored, so a typo like `sandbox_bypas: {enabled: false}` is reported
instead of silently leaving the rule on.
"""

import hashlib
import json
import os

import yaml

from attention_triage import store

SEVERITIES = ("high", "review")
MAX_RETENTION_DAYS = 36500
# Settings of each built-in rule when policy.yaml doesn't name it. Each rule's ticket adds its own.
DEFAULT_RULES = {
    "outside_project_write": {"enabled": True, "severity": "high", "allowed_paths": []},
    "sandbox_bypass": {"enabled": True, "severity": "high"},
    "config_edit": {"enabled": True, "severity": "high", "extra_protected_paths": []},
    "unlisted_domain": {
        "enabled": True,
        "severity": "review",
        "allowed_domains": [
            "github.com",
            "*.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "localhost",
            "127.0.0.1",
            "::1",
        ],
    },
}

DEFAULT_POLICY = """\
# Attention Triage policy. Flags record the policy_version (a hash of the rules' settings) that
# made them. severity: high | review
version: 1
retention_days: 7  # events (and their flags) older than this are deleted
rules:
  outside_project_write:
    enabled: true
    severity: high
    allowed_paths: []  # dirs outside the project the agent may write to, e.g. ~/.cache/myapp
  sandbox_bypass:
    enabled: true
    severity: high
  config_edit:
    enabled: true
    severity: high
    extra_protected_paths: []  # more files or dirs to flag edits of, e.g. ~/.zshrc
  unlisted_domain:
    enabled: true
    severity: review
    # Hosts the agent may reach. `*.example.com` covers subdomains of example.com, not itself.
    # localhost, 127.0.0.1 and ::1 are this machine.
    allowed_domains:
      [github.com, "*.githubusercontent.com", pypi.org, files.pythonhosted.org,
       localhost, 127.0.0.1, "::1"]
"""


class PolicyError(ValueError):
    pass


def path():
    return store.data_dir() / "policy.yaml"


def load() -> tuple[dict, str]:
    """The policy with defaults filled in, and its policy_version. Raises PolicyError if invalid."""
    if not path().exists():
        # Hooks run in parallel: write a temp file and rename it, so none reads a half-written one.
        path().parent.mkdir(parents=True, exist_ok=True)
        temp = path().with_name(f"policy.yaml.{os.getpid()}.tmp")
        temp.write_text(DEFAULT_POLICY)
        temp.replace(path())
    return parse(path().read_text())


def parse(text: str) -> tuple[dict, str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyError(f"not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise PolicyError("expected a mapping with `version` and `rules`")
    # type() check: YAML `true` and `1.0` both equal 1 in Python.
    if type(data.get("version")) is not int or data["version"] != 1:
        raise PolicyError("`version` must be 1")
    check_keys("policy", data, {"version", "rules", "retention_days"})
    retention = data.get("retention_days", store.RETENTION_DAYS)
    # type(): YAML `true` is 1 in Python. At most a century: that keeps everything in practice,
    # while the purge cutoff (now - retention_days) must stay a date Python can represent.
    if type(retention) is not int or not 1 <= retention <= MAX_RETENTION_DAYS:
        raise PolicyError(
            f"`retention_days` must be a whole number of days, from 1 to {MAX_RETENTION_DAYS}"
        )
    # Only an empty value means "defaults": `false` must not quietly leave the rules on.
    rules = {} if data.get("rules") is None else data["rules"]
    if not isinstance(rules, dict):
        raise PolicyError("`rules` must be a mapping of rule id to settings")
    check_keys("rules", rules, DEFAULT_RULES)
    normalized = {"version": 1, "rules": {}}
    for rule_id, default in DEFAULT_RULES.items():
        settings = rules.get(rule_id)
        if settings is None:  # left out, or `sandbox_bypass:` alone
            settings = {}
        if not isinstance(settings, dict):
            raise PolicyError(f"`{rule_id}` must be a mapping of settings")
        check_keys(rule_id, settings, default)
        settings = {**default, **settings}
        if not isinstance(settings["enabled"], bool):
            raise PolicyError(f"`{rule_id}.enabled` must be true or false")
        if settings["severity"] not in SEVERITIES:
            raise PolicyError(f"`{rule_id}.severity` must be one of: {', '.join(SEVERITIES)}")
        for key in ("allowed_paths", "extra_protected_paths"):
            paths = settings.get(key, [])
            # A relative path would resolve against whichever dir the hook happens to run in.
            if not isinstance(paths, list) or not all(
                isinstance(p, str) and p.startswith(("/", "~")) and "\0" not in p for p in paths
            ):
                raise PolicyError(f"`{rule_id}.{key}` must be a list of absolute or ~ paths")
        domains = settings.get("allowed_domains", [])
        # A `*` anywhere but a leading `*.` would never match; report it instead.
        if not isinstance(domains, list) or not all(
            isinstance(d, str) and "*" not in d.removeprefix("*.") for d in domains
        ):
            raise PolicyError(f"`{rule_id}.allowed_domains` must be a list of hosts or *.hosts")
        normalized["rules"][rule_id] = settings
    # The version names the rules' settings that made a flag: keeping events longer changes none.
    canonical = json.dumps(normalized, sort_keys=True)
    return {**normalized, "retention_days": retention}, hashlib.sha256(
        canonical.encode()
    ).hexdigest()[:12]


def check_keys(where: str, found: dict, known) -> None:
    if unknown := [str(key) for key in found if key not in known]:
        raise PolicyError(f"unknown key in {where}: {', '.join(unknown)}")
