#!/usr/bin/env python
"""Privacy audit: read-only assessment of local conversation data safety posture.

Checks (no changes made):
  - Full-disk encryption: BitLocker (Windows), FileVault (macOS), LUKS (Linux)
  - Cloud sync / backup: whether Claude data folders sit inside OneDrive/Dropbox/iCloud/
    Time Machine / File History roots
  - Cleanup settings: cleanupPeriodDays in config, CLAUDE_CODE_SKIP_PROMPT_HISTORY env
  - Data separation: CLAUDE_CONFIG_DIR set (lumo/plain split)
  - Stale data volume: estimated size of projects older than 30 days
  - Update status: latest release tag vs installed version
  - Orca / other tools: copies of session data in their own stores
  - Backup systems: Time Machine (macOS), File History (Windows)

Outputs a short list of findings with severity and a recommendation line for each.
The full audit always runs when asked; only --suggest is rate-limited (weekly).

Modes:
  --suggest: Weekly rate-limited suggestion to run privacy audit
  (default): Full audit output
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

MARKER_FILE = "~/.claude-lumo/.audit-last"
SUGGEST_MARKER_FILE = "~/.claude-lumo/.audit-suggest-last"
SUGGEST_INTERVAL_DAYS = 7  # suggest weekly


def resolve_config_dir():
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = os.path.expanduser(env_dir) if env_dir else os.path.expanduser("~/.claude")
    return os.path.abspath(root)


def format_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def check_encryption():
    """Detect full-disk encryption status."""
    if os.name == "nt":
        # BitLocker: check if the drive containing the config is encrypted
        try:
            drive = os.path.splitdrive(resolve_config_dir())[0] or "C:"
            result = subprocess.run(
                ["Manage-BDE", "-status", drive],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                if "Protection On" in result.stdout:
                    return "bitlocker-on"
                return "bitlocker-off"
        except Exception:
            pass
        return "unknown-windows"

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["fdesetup", "isactive"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip() == "true":
                return "filevault-on"
            return "filevault-off"
        except Exception:
            return "unknown-macos"

    # Linux: check for LUKS
    try:
        result = subprocess.run(
            ["lsblk", "-o", "NAME,MOUNTPOINT,FSTYPE", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for dev in data.get("blockdevices", []):
                fstype = dev.get("fstype", "") or ""
                if "crypto" in fstype.lower() or dev.get("mountpoints"):
                    return "luks-detected"
    except Exception:
        pass
    return "unknown-linux"


def check_cloud_sync(config_dir):
    """Check if config folder is inside any common cloud-synced location."""
    normalized = os.path.normcase(os.path.normpath(os.path.abspath(config_dir)))

    # Windows common paths
    if os.name == "nt":
        candidates = [
            os.path.expandvars("$OneDrive"),
            os.path.expandvars("$USERPROFILE\\OneDrive"),
            os.path.expandvars("$APPDATA\\..\\OneDrive"),
            os.path.expandvars("$USERPROFILE\\Dropbox"),
            os.path.expandvars("$APPDATA\\Dropbox"),
            os.path.expandvars("$LOCALAPPDATA\\Dropbox"),
        ]
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, "Library", "Mobile Documents"),  # iCloud
            os.path.join(home, "Dropbox"),
            os.path.join(home, ".dropbox"),
            os.path.join(home, "Google Drive"),
        ]

    found = []
    for cand in candidates:
        try:
            cand = os.path.normcase(os.path.normpath(cand))
            if cand and (normalized == cand or normalized.startswith(cand + os.sep)):
                found.append(cand)
        except Exception:
            pass

    return found


def check_backup_system(config_dir):
    """Check whether a local backup system keeps copies of Claude data.

    Returns (name, detail) for the first detected system, or (None, None).
    Read-only: only checks for the presence of a backup configuration, never
    runs a backup or reads any backup contents.
    """
    # macOS: Time Machine destinations (tmutil is the supported query)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["tmutil", "destinationinfo"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "No destinations configured" not in result.stdout:
                return "Time Machine", "tmutil reports configured destinations; " \
                    "backups may retain deleted conversations indefinitely."
        except Exception:
            pass
        return None, None

    # Windows: File History config lives under %LOCALAPPDATA%\Microsoft\Windows\FileHistory
    if os.name == "nt":
        fh_root = os.path.expandvars(r"$LOCALAPPDATA\Microsoft\Windows\FileHistory")
        if os.path.isdir(fh_root):
            return "File History", f"config found at {fh_root}; backups may retain " \
                "deleted conversations until aged out (default 5% disk or 6 months)."
        return None, None

    # Linux: no standard system to probe for; leave to the user
    return None, None


def check_settings(config_dir):
    """Read relevant settings from config."""
    settings_file = os.path.join(config_dir, "settings.json")
    settings = {"cleanupPeriodDays": None}

    if os.path.isfile(settings_file):
        try:
            with open(settings_file, encoding="utf-8") as f:
                data = json.load(f)
            settings["cleanupPeriodDays"] = data.get("cleanupPeriodDays")
        except (ValueError, OSError):
            pass

    settings["dataSeparation"] = bool(os.environ.get("CLAUDE_CONFIG_DIR"))

    # Check env var
    settings["skipPromptHistory"] = bool(os.environ.get("CLAUDE_CODE_SKIP_PROMPT_HISTORY"))

    return settings


def estimate_stale_size(config_dir, days=30):
    """Estimate total size of projects older than the threshold."""
    projects_root = os.path.join(config_dir, "projects")
    if not os.path.isdir(projects_root):
        return 0, 0

    cutoff = time.time() - days * 86400
    total = 0
    stale_count = 0

    try:
        names = os.listdir(projects_root)
    except OSError:
        return 0, 0

    for project in names:
        folder = os.path.join(projects_root, project)
        if not os.path.isdir(folder):
            continue
        try:
            mtimes = [os.path.getmtime(os.path.join(folder, f))
                      for f in os.listdir(folder)
                      if os.path.isfile(os.path.join(folder, f))]
        except OSError:
            mtimes = []
        if mtimes and max(mtimes) < cutoff:
            stale_count += 1
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    total += os.path.getsize(path)
                elif os.path.isdir(path):
                    for root, dirs, files in os.walk(path):
                        for fn in files:
                            try:
                                total += os.path.getsize(os.path.join(root, fn))
                            except OSError:
                                pass

    return stale_count, total


def check_update_status():
    """Compare installed version to latest release."""
    try:
        repo = os.path.join(os.path.dirname(os.path.dirname(__file__)))
        if not os.path.isdir(os.path.join(repo, ".git")):
            return None, None, None

        result = subprocess.run(
            ["git", "-C", repo, "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        installed = result.stdout.strip() if result.returncode == 0 else None

        result = subprocess.run(
            ["git", "-C", repo, "fetch", "--quiet", "origin", "refs/tags/*:refs/tags/*"],
            capture_output=True, text=True, timeout=10
        )

        result = subprocess.run(
            ["git", "-C", repo, "tag", "-l"],
            capture_output=True, text=True, timeout=5
        )
        tags = [t.strip() for t in result.stdout.splitlines()
                if t.strip() and t.strip().startswith("v")]
        latest = max(tags) if tags else None

        behind = 0
        if installed and latest and installed != latest:
            result = subprocess.run(
                ["git", "-C", repo, "rev-list", f"{installed}..{latest}", "--count"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                behind = int(result.stdout.strip())

        return installed, latest, behind
    except Exception:
        return None, None, None


def describe_orca(orca_dir):
    """Report on Orca's data stores."""
    found = []
    entries = [
        ("terminal scrollback", os.path.join(orca_dir, "terminal-history")),
        ("agent session runtimes", os.path.join(orca_dir, "agent-session-runtimes")),
        ("session recovery", os.path.join(orca_dir, "agent-session-recovery.json")),
        ("orchestration DB", os.path.join(orca_dir, "orchestration.db")),
    ]
    for label, path in entries:
        if os.path.exists(path):
            size = sum(
                os.path.getsize(os.path.join(root, f))
                for root, dirs, files in os.walk(path) if os.path.isdir(path)
                for f in files
            ) if os.path.isdir(path) else (os.path.getsize(path) if os.path.isfile(path) else 0)
            found.append((label, size))
    return found


def print_finding(severity, title, details):
    # ASCII labels only: Windows consoles default to cp1252 and mangle dot/dash icons.
    sev_label = {"low": "L", "medium": "M", "high": "H"}.get(severity, "?")
    print(f"  [{sev_label}] {title}")
    if details:
        for line in details:
            print(f"      {line}")


def main():
    parser = argparse.ArgumentParser(description="Privacy audit for claude-lumo")
    parser.add_argument("--suggest", action="store_true",
        help="Weekly rate-limited suggestion to run privacy audit (quiet mode)")
    parser.add_argument("--days", type=int, default=30,
        help="Staleness threshold in days (default: 30)")
    args = parser.parse_args()

    config_dir = resolve_config_dir()

    # Handle --suggest mode separately
    if args.suggest:
        marker = os.path.expanduser(SUGGEST_MARKER_FILE)
        try:
            mtime = os.path.getmtime(marker)
        except OSError:
            mtime = None
        if mtime and (time.time() - mtime) < SUGGEST_INTERVAL_DAYS * 86400:
            return  # Silent when recently suggested
        # Always touch the suggest marker after checking
        os.makedirs(os.path.dirname(os.path.expanduser(SUGGEST_MARKER_FILE)), exist_ok=True)
        try:
            with open(os.path.expanduser(SUGGEST_MARKER_FILE), "w") as mf:
                mf.write("")
        except OSError:
            pass
        # Check if there's anything to suggest
        stale_count, _ = estimate_stale_size(config_dir, args.days)
        if stale_count > 0:
            print(f"Privacy tip: {stale_count} stale project(s) found. "
                  f"Run `claude-lumo privacy-audit` for a full assessment.")
        return

    # The full audit runs whenever the user asks for it; only the --suggest
    # reminder is rate-limited (it is meant for automated/hook invocation).
    findings = []

    # Encryption check
    enc_status = check_encryption()
    if enc_status in ("bitlocker-off", "filevault-off"):
        findings.append({
            "severity": "high",
            "title": "Full-disk encryption appears disabled",
            "details": [
                "Your Claude data folders are stored unencrypted on disk.",
                "Consider enabling BitLocker (Windows) or FileVault (macOS)."
            ]
        })
    elif enc_status.startswith("unknown"):
        findings.append({
            "severity": "low",
            "title": "Could not determine encryption status",
            "details": ["Manually verify your disk encryption is enabled."]
        })

    # Cloud sync check
    synced_roots = check_cloud_sync(config_dir)
    if synced_roots:
        findings.append({
            "severity": "medium",
            "title": "Claude data is in a cloud-synced folder",
            "details": [f"Found under: {', '.join(synced_roots)}"] + [
                "Cloud sync may expose transcripts to cloud storage.",
                "Consider moving CLAUDE_CONFIG_DIR outside these folders."
            ]
        })
    else:
        findings.append({
            "severity": "low",
            "title": "Data folder not in cloud sync (good)",
            "details": ["Claude data appears to be stored locally only."]
        })

    # Backup system check
    backup_name, backup_detail = check_backup_system(config_dir)
    if backup_name:
        findings.append({
            "severity": "medium",
            "title": f"{backup_name} may retain deleted conversations",
            "details": [backup_detail]
        })

    # Settings check
    settings = check_settings(config_dir)
    if settings.get("cleanupPeriodDays") is None:
        findings.append({
            "severity": "medium",
            "title": "No automatic cleanup configured",
            "details": [
                "cleanupPeriodDays is not set in settings.json.",
                "Run `claude-lumo wipe` periodically, or set a cleanup period."
            ]
        })

    # Data separation
    if not settings.get("dataSeparation"):
        findings.append({
            "severity": "low",
            "title": "Lumo/plain data separation not enabled",
            "details": [
                "CLAUDE_CONFIG_DIR is not set, so Lumo and plain Claude share the same data store.",
                "Set CLAUDE_CONFIG_DIR to ~/.claude-lumo for separation."
            ]
        })

    # Stale data check
    stale_count, stale_size = estimate_stale_size(config_dir, args.days)
    if stale_count > 0:
        findings.append({
            "severity": "medium",
            "title": f"{stale_count} stale project(s) found",
            "details": [
                f"Approximately {format_size(stale_size)} of data is over {args.days} days old.",
                "Run `claude-lumo wipe` to review and delete stale conversations."
            ]
        })

    # Update status
    installed, latest, behind = check_update_status()
    if installed and latest and behind and behind > 0:
        findings.append({
            "severity": "low",
            "title": f"Update available ({behind} commits behind)",
            "details": [
                f"Installed: {installed}, latest: {latest}",
                "Consider updating via the installer script."
            ]
        })

    # Orca copies
    orca_dir = os.environ.get("APPDATA")
    if orca_dir:
        orca_dir = os.path.join(os.path.expanduser(orca_dir), "orca")
    orca_found = describe_orca(os.path.abspath(orca_dir) if orca_dir else "/dev/null")
    if orca_found:
        total = sum(size for _, size in orca_found)
        findings.append({
            "severity": "low",
            "title": "Orca maintains additional session data",
            "details": [
                f"Orca stores ~{format_size(total)} in its own data folder.",
                "See the wipe command output for manual cleanup steps."
            ]
        })

    # Print summary
    print("\nPrivacy Audit Results\n")
    high = sum(1 for f in findings if f["severity"] == "high")
    med = sum(1 for f in findings if f["severity"] == "medium")
    low = sum(1 for f in findings if f["severity"] == "low")
    print(f"Summary: {high} high, {med} medium, {low} low priority findings\n")

    for f in findings:
        print_finding(f["severity"], f["title"], f["details"])

    if not findings:
        print("  [.] No issues found")

    # Record that a full audit ran (informational; does not block future runs)
    os.makedirs(os.path.dirname(os.path.expanduser(MARKER_FILE)), exist_ok=True)
    try:
        with open(os.path.expanduser(MARKER_FILE), "w") as mf:
            mf.write("")
    except OSError:
        pass


if __name__ == "__main__":
    main()
