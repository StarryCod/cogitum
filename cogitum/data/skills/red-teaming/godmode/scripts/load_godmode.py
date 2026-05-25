
# ──────────────────────────────────────────────────────────────────
# Approval gate — see _godmode_gate.py. MUST run before any other
# import so the script aborts cleanly even if upstream deps are
# missing (yaml, anthropic, …).
# ──────────────────────────────────────────────────────────────────
import os as _gm_os
import sys as _gm_sys
from pathlib import Path as _gm_Path
_gm_gate_dir = _gm_Path(_gm_os.getenv("HERMES_HOME", _gm_Path.home() / ".hermes")) / "skills" / "red-teaming" / "godmode" / "scripts"
_gm_sys.path.insert(0, str(_gm_gate_dir))
try:
    from _godmode_gate import require_consent as _gm_require_consent
    _gm_require_consent("load_godmode")
finally:
    try:
        _gm_sys.path.remove(str(_gm_gate_dir))
    except ValueError:
        pass


import os, sys
from pathlib import Path


# _godmode_gate.py for rationale. Bypassed only when the operator
# sets COGITUM_GODMODE_CONFIRMED=1 or types 'I AGREE' interactively.
# ──────────────────────────────────────────────────────────────────
_gm_gate_dir = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "skills" / "red-teaming" / "godmode" / "scripts"
sys.path.insert(0, str(_gm_gate_dir))
try:
    from _godmode_gate import require_consent as _gm_require_consent
    _gm_require_consent("load_godmode")
finally:
    try:
        sys.path.remove(str(_gm_gate_dir))
    except ValueError:
        pass

_gm_scripts_dir = _gm_gate_dir

_gm_old_argv = sys.argv
sys.argv = ["_godmode_loader"]

def _gm_load(path):
    ns = dict(globals())
    ns["__name__"] = "_godmode_module"
    ns["__file__"] = str(path)
    exec(compile(open(path).read(), str(path), 'exec'), ns)
    return ns

for _gm_script in ["parseltongue.py", "godmode_race.py", "auto_jailbreak.py"]:
    _gm_path = _gm_scripts_dir / _gm_script
    if _gm_path.exists():
        _gm_ns = _gm_load(_gm_path)
        for _gm_k, _gm_v in _gm_ns.items():
            if not _gm_k.startswith('_gm_') and (callable(_gm_v) or _gm_k.isupper()):
                globals()[_gm_k] = _gm_v

sys.argv = _gm_old_argv

# Cleanup loader vars
for _gm_cleanup in ['_gm_scripts_dir', '_gm_old_argv', '_gm_load', '_gm_ns', '_gm_k',
                     '_gm_v', '_gm_script', '_gm_path', '_gm_cleanup']:
    globals().pop(_gm_cleanup, None)
