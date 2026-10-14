import pytest

from attention_triage import policy


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def write_policy(text: str) -> None:
    policy.path().parent.mkdir()
    policy.path().write_text(text)


def test_a_missing_policy_is_created_with_the_defaults():  # and its data dir
    loaded, version = policy.load()
    assert policy.path().read_text() == policy.DEFAULT_POLICY
    assert loaded["rules"] == {
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
    assert version == policy.parse(policy.DEFAULT_POLICY)[1]


def test_an_existing_policy_is_read_not_replaced():
    write_policy("version: 1\nrules:\n  sandbox_bypass:\n    severity: review\n")
    loaded, _ = policy.load()
    assert loaded["rules"]["sandbox_bypass"] == {"enabled": True, "severity": "review"}
    assert "review" in policy.path().read_text()


@pytest.mark.parametrize("rules_yaml", ["", "rules:\n", "rules:\n  sandbox_bypass:\n"])
def test_a_rule_left_out_gets_its_defaults(rules_yaml):
    assert policy.parse("version: 1\n" + rules_yaml) == policy.parse(policy.DEFAULT_POLICY)


def test_version_ignores_comments_layout_and_key_order():
    a = "version: 1\nrules:\n  sandbox_bypass:\n    enabled: true\n    severity: review\n"
    b = "# mine\nrules: {sandbox_bypass: {severity: review,   enabled: true}}\nversion: 1\n"
    assert policy.parse(a)[1] == policy.parse(b)[1]


def test_version_changes_when_a_setting_changes():
    a = "version: 1\nrules:\n  sandbox_bypass:\n    enabled: true\n"
    b = "version: 1\nrules:\n  sandbox_bypass:\n    enabled: false\n"
    assert policy.parse(a)[1] != policy.parse(b)[1]


@pytest.mark.parametrize(
    "text",
    [
        "version: [1",  # not YAML
        "- 1\n",  # not a mapping
        "rules: {}\n",  # no version
        "version: 2\n",
        "version: 1\nretention: 3\n",  # unknown key
        "version: 1\nrules: [sandbox_bypass]\n",
        "version: 1\nrules: false\n",
        "version: 1\nrules:\n  sandbox_bypass: false\n",  # must not mean "defaults"
        "version: 1\nrules:\n  sandbox_bypas: {enabled: false}\n",  # typo'd rule id
        "version: 1\nrules:\n  sandbox_bypass: true\n",
        "version: 1\nrules:\n  sandbox_bypass: {enabled: 'no'}\n",
        "version: 1\nrules:\n  sandbox_bypass: {severity: low}\n",
        "version: 1\nrules:\n  sandbox_bypass: {enable: false}\n",  # typo'd setting
        "version: 1\nrules:\n  outside_project_write: {allowed_paths: ~/x}\n",  # not a list
        "version: 1\nrules:\n  outside_project_write: {allowed_paths: [1]}\n",
        "version: 1\nrules:\n  outside_project_write: {allowed_paths: [../shared]}\n",
        "version: 1\nrules:\n  config_edit: {extra_protected_paths: [.zshrc]}\n",
        'version: 1\nrules:\n  config_edit: {extra_protected_paths: ["/a\\0b"]}\n',  # a NUL
        "version: 1\nrules:\n  unlisted_domain: {allowed_domains: github.com}\n",  # not a list
        "version: 1\nrules:\n  unlisted_domain: {allowed_domains: [1]}\n",
        "version: 1\nrules:\n  unlisted_domain: {allowed_domains: ['*github.com']}\n",
        "version: 1\nrules:\n  unlisted_domain: {allowed_domains: ['api.*.example']}\n",
    ],
)
def test_an_invalid_policy_is_rejected(text):
    with pytest.raises(policy.PolicyError):
        policy.parse(text)


def test_retention_days_defaults_to_7_and_doesnt_change_the_version():
    loaded, version = policy.parse(policy.DEFAULT_POLICY)
    assert loaded["retention_days"] == 7
    left_out, same = policy.parse(policy.DEFAULT_POLICY.replace("retention_days: 7", ""))
    assert left_out["retention_days"] == 7
    longer, also_same = policy.parse(
        policy.DEFAULT_POLICY.replace("retention_days: 7", "retention_days: 30")
    )
    assert longer["retention_days"] == 30
    assert version == same == also_same  # the version names the rules' settings


def test_retention_days_can_keep_a_century():
    century = policy.DEFAULT_POLICY.replace("retention_days: 7", "retention_days: 36500")
    assert policy.parse(century)[0]["retention_days"] == 36500


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true", "seven", "[]", "36501", "1000000"])
def test_retention_days_must_be_a_whole_number_of_days(value):
    with pytest.raises(policy.PolicyError, match="retention_days"):
        policy.parse(policy.DEFAULT_POLICY.replace("retention_days: 7", f"retention_days: {value}"))
