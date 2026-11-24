"""Host checks for the os-image lane: boot configuration, update identity,
kernel and image settings, the organ units and the board scripts.

These run on the Mac without Buildroot. Anything that needs the builder, a VM
or the laptop is named in the plan, not faked here.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OS = ROOT / "os"
BOARD = OS / "board/beingos"
PACKAGE = OS / "package/being"
SLOTS = BOARD / "slots.sh"


def render(template, variant, ids=False):
    """The same rendering post-image.sh, post-build.sh and try-image.sh do."""
    script = '. "$1"; if [ "$3" = 1 ]; then slot_ids "$2"; else cat "$2"; fi | boot_console'
    env = dict(os.environ, BEINGOS_VARIANT=variant)
    result = subprocess.run(["/bin/sh", "-c", script, "render", str(SLOTS), str(template), "1" if ids else "0"],
                            env=env, text=True, capture_output=True, check=True, timeout=5)
    return result.stdout


def kernel_lines(config):
    return [line for line in config.splitlines() if re.match(r"\s*(if )?linux ", line.strip())]


def extra_args(config):
    return dict(re.findall(r'set extra="([^"]*)"', config) and
                zip(("messages", "safe", "normal"), re.findall(r'set extra="([^"]*)"', config)))


def find_openssl3():
    for candidate in (os.environ.get("OPENSSL"), "/opt/homebrew/opt/openssl@3/bin/openssl",
                      "/usr/local/opt/openssl@3/bin/openssl", shutil.which("openssl")):
        if not candidate or not Path(candidate).exists():
            continue
        out = subprocess.run([candidate, "version"], capture_output=True, text=True).stdout
        if re.match(r"OpenSSL [3-9]", out):
            return candidate
    return None


class GrubTests(unittest.TestCase):
    def test_laptop_boots_never_panic_on_an_oops_or_name_the_serial_line(self):
        for template in (BOARD / "grub.cfg", BOARD / "try-grub.cfg"):
            with self.subTest(template=template.name):
                config = render(template, "laptop")
                self.assertNotIn("oops=panic", config)
                self.assertNotIn("ttyS0", config)
                self.assertNotRegex(config, r"(?m)^serial |^terminal_output serial")
                self.assertNotRegex(config, r"BEING_[A-Z]+_ARGS")

    def test_lab_keeps_its_serial_console_and_oops_panic_and_drops_the_tokens(self):
        config = render(BOARD / "grub.cfg", "lab", ids=True)
        self.assertRegex(config, r"(?m)^serial --unit=0")
        normal = extra_args(config)["normal"]
        self.assertEqual(normal.split(), ["loglevel=3", "panic=10", "oops=panic", "console=ttyS0,115200"])
        self.assertNotRegex(config, r"BEING_[A-Z]+_ARGS|hid_apple|vt.global_cursor_default")

    def test_every_laptop_kernel_line_keeps_the_printed_top_row_and_msr_read_only(self):
        # fnmode=1: the top row does what is printed on it; fn+key is F1-F12.
        installed = render(BOARD / "grub.cfg", "laptop")
        for mode, args in extra_args(installed).items():
            with self.subTest(mode=mode):
                self.assertIn("hid_apple.fnmode=1", args.split())
                self.assertNotIn("hid_apple.fnmode=2", args.split())
                self.assertIn("msr.allow_writes=off", args.split())
        try_config = render(BOARD / "try-grub.cfg", "laptop")
        lines = kernel_lines(try_config)
        # Normal, messages and safe graphics, then the round-2 entries
        # (two microphone quirks and the measuring boot) once they exist.
        self.assertIn(len(lines), (3, 6), lines)
        for line in lines:
            self.assertIn("hid_apple.fnmode=1", line.split())
            self.assertIn("msr.allow_writes=off", line)

    def test_only_the_normal_installed_boot_is_quiet(self):
        args = extra_args(render(BOARD / "grub.cfg", "laptop"))
        for word in ("quiet", "vt.global_cursor_default=0", "systemd.show_status=error"):
            self.assertIn(word, args["normal"].split())
            self.assertNotIn(word, args["messages"].split())
            self.assertNotIn(word, args["safe"].split())
        for mode in ("messages", "safe"):
            for word in ("panic=0", "systemd.watchdog_sec=off", "being.verbose=1", "console=tty0"):
                self.assertIn(word, args[mode].split(), mode)

    def test_installed_menu_is_hidden_with_three_rescue_entries(self):
        config = (BOARD / "grub.cfg").read_text()
        block = config[config.index('if [ -n "$install_esp" ]; then\n    # An installed'):]
        self.assertIn("set timeout_style=hidden", block)
        self.assertIn("set timeout=1", block)
        titles = re.findall(r'menuentry "([^"]+)"(?: --hotkey=(\w))?', config)
        self.assertEqual(titles, [("PipOS", ""), ("PipOS (previous slot)", "p"),
                                  ("PipOS (show messages)", "v"), ("PipOS (safe graphics)", "s")])
        # The lab factory image boots its first entry at once, as before.
        self.assertRegex(config, r"(?m)^set default=0\nset timeout=0$")

    def test_unconfirmed_boot_counter_counts_only_the_confirmed_installed_slot(self):
        config = (BOARD / "grub.cfg").read_text()
        self.assertIn("load_env -f \"$envfile\" ORDER GOOD A_OK A_TRY A_LEFT B_OK B_TRY B_LEFT PIP_UNSEEN", config)
        branch = config[config.index('    elif [ -n "$install_esp" ]; then'):config.index('    if [ "$mode" = messages ]')]
        # 0 -> 1 -> 2 -> 3, then the show-messages boot.
        self.assertEqual(re.findall(r"set PIP_UNSEEN=(\d)", branch), ["1", "2", "3", "3"])
        self.assertIn("set mode=messages", branch)
        self.assertIn("save_env -f \"$envfile\" PIP_UNSEEN", branch)
        # Trial boots keep their own attempts and never touch the counter.
        trial = config[config.index('    elif [ "$1" != "$GOOD" ]; then'):config.index('    elif [ -n "$install_esp" ]; then')]
        self.assertNotIn("PIP_UNSEEN", trial)
        unit = (PACKAGE / "being-boot-health.service").read_text()
        self.assertIn("ExecStopPost=-/bin/sh -c '[ -f /run/being-slot-good ] || exit 0;", unit)
        self.assertIn("grub-editenv \"$$env\" set PIP_UNSEEN=0", unit)

    def test_grub_scripts_balance_their_blocks(self):
        for template in (BOARD / "grub.cfg", BOARD / "try-grub.cfg"):
            for variant in ("lab", "laptop"):
                text = render(template, variant)
                words = re.findall(r"(?m)^\s*(if|fi|function|menuentry|for|done)\b", text)
                self.assertEqual(words.count("if"), words.count("fi"), (template.name, variant))
                self.assertEqual(words.count("for"), words.count("done"), (template.name, variant))
                self.assertEqual(text.count("{"), text.count("}"), (template.name, variant))


class _Shim:
    """GNU `install -D -m MODE SRC DST` for the Mac's BSD userland."""

    INSTALL = r'''#!/bin/sh
mode=0755
while [ $# -gt 2 ]; do
    case "$1" in -D) shift ;; -m) mode=$2; shift 2 ;; *) break ;; esac
done
mkdir -p "$(dirname "$2")" && cp "$1" "$2" && chmod "$mode" "$2"
'''


class PostBuildTests(unittest.TestCase):
    def setUp(self):
        self.openssl = find_openssl3()
        if not self.openssl:
            self.skipTest("needs OpenSSL 3 on the Mac")
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        (self.bin / "install").write_text(_Shim.INSTALL)
        (self.bin / "install").chmod(0o755)
        (self.bin / "openssl").symlink_to(self.openssl)
        self.lab = self.certificate("PipOS Lab Updates", "lab")

    def certificate(self, cn, name):
        folder = self.work / name
        folder.mkdir()
        subprocess.run([self.openssl, "req", "-new", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "30",
                        "-subj", f"/CN={cn}", "-keyout", str(folder / "key.pem"), "-out", str(folder / "cert.pem")],
                       check=True, capture_output=True, timeout=60)
        return folder

    def tree(self, *, model_pack_cert=True):
        target = Path(tempfile.mkdtemp(dir=self.work))
        dynload = target / "usr/lib/python3.13/lib-dynload"
        dynload.mkdir(parents=True)
        (dynload / "_ssl.cpython-313-x86_64-linux-gnu.so").write_bytes(b"\0")
        (target / "etc/systemd/system").mkdir(parents=True)
        (target / "etc/being").mkdir()
        (target / "etc/fstab").write_text("/dev/root / ext2 rw 0 1\n")
        mind = target / "usr/lib/being/mind"
        mind.mkdir(parents=True)
        (mind / "model_pack.py").write_text('CERT = "/usr/share/being/model-pack.pem"\n' if model_pack_cert
                                            else 'CERT = "/etc/rauc/keyring.pem"\n')
        binaries = Path(tempfile.mkdtemp(dir=self.work))
        (binaries / "bzImage").write_bytes(b"kernel")
        (binaries / "efi-part/EFI/BOOT").mkdir(parents=True)
        (binaries / "efi-part/EFI/BOOT/bootx64.efi").write_bytes(b"efi")
        return target, binaries

    def post_build(self, variant, *, release=None, require=False, model_pack_cert=True):
        target, binaries = self.tree(model_pack_cert=model_pack_cert)
        env = dict(os.environ, TARGET_DIR=str(target), BINARIES_DIR=str(binaries),
                   BEINGOS_LAB_KEYS=str(self.lab), PATH=f"{self.bin}:{os.environ['PATH']}",
                   BEINGOS_RELEASE_CERT=str(release / "cert.pem") if release else "",
                   BEINGOS_REQUIRE_RELEASE_KEY="1" if require else "0")
        result = subprocess.run(["/bin/sh", str(BOARD / "post-build.sh"), str(target), variant],
                                env=env, text=True, capture_output=True, timeout=60)
        return result, target

    def read(self, target, path):
        return (target / path).read_text()

    def test_lab_image_keeps_the_lab_identity(self):
        result, target = self.post_build("lab")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compatible=beingos-lab-v1\n", self.read(target, "etc/rauc/system.conf"))
        self.assertIn("compatible=beingos-installed-v1\n", self.read(target, "usr/share/being/installed-rauc.conf"))
        self.assertEqual(self.read(target, "usr/share/being/variant"), "lab\n")
        self.assertIn("trust=lab\n", self.read(target, "usr/share/being/update-trust"))
        self.assertNotIn("allowed-signer-cns", self.read(target, "etc/rauc/system.conf"))
        self.assertIn("use-bundle-signing-time=true", self.read(target, "etc/rauc/system.conf"))
        self.assertTrue((target / "workshop").is_dir())
        self.assertFalse((target / "usr/lib/modules-load.d/being-msr.conf").exists())
        # system.conf has the real slot IDs; the installed template keeps its tokens.
        self.assertNotIn("SLOT_A_UUID", self.read(target, "etc/rauc/system.conf"))
        self.assertIn("SLOT_A_UUID", self.read(target, "usr/share/being/installed-rauc.conf"))

    def test_laptop_image_has_its_own_compatible_strings(self):
        result, target = self.post_build("laptop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: no owner release certificate", result.stderr)
        self.assertIn("compatible=beingos-laptop-v1\n", self.read(target, "etc/rauc/system.conf"))
        self.assertIn("compatible=beingos-laptop-installed-v1\n",
                      self.read(target, "usr/share/being/installed-rauc.conf"))
        self.assertEqual(self.read(target, "usr/share/being/variant"), "laptop\n")
        trust = self.read(target, "usr/share/being/update-trust")
        self.assertIn("trust=lab\n", trust)
        self.assertRegex(trust, r"certificate_sha256=[0-9a-f]{64}\n")
        self.assertIn("msr", self.read(target, "usr/lib/modules-load.d/being-msr.conf").split())
        self.assertEqual(os.readlink(target / "etc/systemd/system/getty@.service"), "/dev/null")
        installed = self.read(target, "usr/share/being/installed-grub.cfg")
        self.assertNotIn("oops=panic", installed)
        self.assertNotIn("ttyS0", installed)

    def test_an_image_for_installing_refuses_the_lab_key(self):
        result, _ = self.post_build("laptop", require=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no release certificate", result.stderr)

    def test_release_certificate_is_the_only_update_trust(self):
        release = self.certificate("PipOS Laptop Release", "release")
        result, target = self.post_build("laptop", release=release, require=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read(target, "etc/rauc/keyring.pem"), (release / "cert.pem").read_text())
        self.assertEqual(self.read(target, "usr/share/being/model-pack.pem"), (self.lab / "cert.pem").read_text())
        self.assertIn("trust=release\n", self.read(target, "usr/share/being/update-trust"))
        for path in ("etc/rauc/system.conf", "usr/share/being/installed-rauc.conf"):
            lines = self.read(target, path).splitlines()
            self.assertIn("allowed-signer-cns=PipOS Laptop Release", lines, path)
            self.assertEqual(lines.index("allowed-signer-cns=PipOS Laptop Release"),
                             lines.index("use-bundle-signing-time=true") + 1)

    def test_release_certificate_needs_its_name_and_a_model_pack_certificate(self):
        wrong = self.certificate("Somebody Else", "wrong")
        result, _ = self.post_build("laptop", release=wrong)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected CN=PipOS Laptop Release", result.stderr)
        release = self.certificate("PipOS Laptop Release", "release")
        result, _ = self.post_build("laptop", release=release, model_pack_cert=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model pack against the update keyring", result.stderr)

    def test_unknown_variant_is_refused(self):
        result, _ = self.post_build("desktop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown PipOS variant", result.stderr)


class UpdateReadyTests(unittest.TestCase):
    def run_hook(self, args, variant=None, env=None, ready=True, good=None):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (work / "cmdline").write_text(args + "\n")
        variant_file = work / "variant"
        if variant:
            variant_file.write_text(variant + "\n")
        marker = work / "ready"
        if ready:
            marker.touch()
        fake = work / "bin"
        fake.mkdir()
        (fake / "grub-editenv").write_text("#!/bin/sh\n" + (f"echo GOOD={good}\n" if good else "exit 0\n"))
        (fake / "grub-editenv").chmod(0o755)
        script = (BOARD / "being-update-ready").read_text()
        for old, new in (("/proc/cmdline", work / "cmdline"), ("/usr/share/being/variant", variant_file),
                         ("/run/being-slot-good", marker)):
            script = script.replace(old, shlex.quote(str(new)))
        result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, timeout=5,
                                env=dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", **(env or {})))
        return result

    def test_a_lab_bundle_never_installs_on_the_laptop(self):
        for bundle, allowed in (("beingos-laptop-installed-v1", True), ("beingos-installed-v1", False),
                                ("beingos-lab-v1", False)):
            with self.subTest(bundle=bundle):
                result = self.run_hook("rauc.slot=A", "laptop", {"RAUC_MF_COMPATIBLE": bundle})
                self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_a_laptop_bundle_never_installs_on_the_lab(self):
        result = self.run_hook("rauc.slot=A", "lab", {"RAUC_MF_COMPATIBLE": "beingos-laptop-v1"})
        self.assertNotEqual(result.returncode, 0)
        result = self.run_hook("rauc.slot=A", None, {"RAUC_MF_COMPATIBLE": "beingos-lab-v1",
                                                     "RAUC_SYSTEM_COMPATIBLE": "beingos-lab-v1"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bundle_must_match_the_system_compatible(self):
        result = self.run_hook("rauc.slot=A", "laptop", {"RAUC_MF_COMPATIBLE": "beingos-laptop-v1",
                                                         "RAUC_SYSTEM_COMPATIBLE": "beingos-laptop-installed-v1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different kind of PipOS", result.stderr)

    def test_a_degraded_confirmed_slot_may_still_be_updated(self):
        self.assertEqual(self.run_hook("rauc.slot=B", "lab", ready=False, good="B").returncode, 0)
        trial = self.run_hook("rauc.slot=B being.trial=1", "lab", ready=False, good="A")
        self.assertNotEqual(trial.returncode, 0)
        self.assertIn("health check", trial.stderr)


class InstalledGeneratorExtrasTests(unittest.TestCase):
    GUIDS = {field: f"00000000-0000-4000-8000-{number:012d}"
             for number, field in enumerate(("esp", "slot_a", "slot_b", "models", "mind"), 1)}

    def args(self):
        active = self.GUIDS["slot_a"]
        base = f"root=PARTUUID={active} ro rauc.slot=A being.installed=1 fstab=no systemd.gpt_auto=no"
        return base + " " + " ".join(f"being.{field}={guid}" for field, guid in self.GUIDS.items())

    def generate(self, args):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (work / "cmdline").write_text(args + "\n")
        outputs = [work / name for name in ("normal", "early", "late")]
        for output in outputs:
            output.mkdir()
        script = (PACKAGE / "being-installed-generator").read_text().replace("/proc/cmdline", shlex.quote(str(work / "cmdline")))
        script = script.replace("/usr/share/being/installed-rauc.conf", shlex.quote(str(BOARD / "rauc.conf")))
        (work / "systemd-escape").write_text('#!/bin/sh\nprintf "x-%s.device\\n" "$(echo "$3" | tr / -)"\n')
        (work / "systemd-escape").chmod(0o755)
        subprocess.run(["/bin/sh", "-c", script, "gen", *map(str, outputs)], check=True, capture_output=True,
                       timeout=6, env=dict(os.environ, PATH=f"{work}:{os.environ['PATH']}"))
        return outputs[1]

    def test_only_mind_and_var_are_required_at_boot(self):
        early = self.generate(self.args())
        self.assertEqual(sorted(p.name for p in (early / "local-fs.target.requires").iterdir()),
                         ["mind.mount", "var.mount"])
        self.assertEqual(sorted(p.name for p in (early / "local-fs.target.wants").iterdir()),
                         ["bootstate.mount", "models.mount", "var-log-journal.mount"])
        self.assertIn("nofail", (early / "bootstate.mount").read_text())

    def test_installed_logs_live_on_the_mind_partition(self):
        early = self.generate(self.args())
        journal = (early / "var-log-journal.mount").read_text()
        self.assertIn("What=/mind/log/journal\nWhere=/var/log/journal\nType=none\nOptions=bind", journal)
        self.assertIn("Before=systemd-journal-flush.service", journal)
        record = (early / "being-boot-record.service").read_text()
        self.assertIn("ExecStart=/usr/libexec/being-boot-record", record)
        self.assertIn("After=mind.mount", record)

    def test_a_bad_identity_still_gets_a_writable_var_and_a_trial_reboots(self):
        bad = self.args().replace("fstab=no", "fstab=yes")
        early = self.generate(bad)
        self.assertIn("What=tmpfs\nWhere=/var", (early / "var.mount").read_text())
        self.assertFalse((early / "being-installed-deny-reboot.service").exists())
        early = self.generate(bad + " being.trial=1")
        self.assertTrue((early / "multi-user.target.wants/being-installed-deny-reboot.service").exists())
        self.assertEqual((early / "being-boot-health.service").readlink(), Path("/dev/null"))


class BoardScriptTests(unittest.TestCase):
    def test_post_fakeroot_strips_every_setuid_and_setgid_bit(self):
        target = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (target / "bin").mkdir()
        busybox = target / "bin/busybox"
        busybox.write_text("#!/bin/sh\n")
        busybox.chmod(0o4755)
        other = target / "bin/wall"
        other.write_text("x")
        other.chmod(0o2755)
        result = subprocess.run(["/bin/sh", str(BOARD / "post-fakeroot.sh"), str(target), "laptop"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (busybox, other):
            mode = path.stat().st_mode
            self.assertFalse(mode & (stat.S_ISUID | stat.S_ISGID), path)
            self.assertTrue(mode & stat.S_IXUSR)
        self.assertIn("removed setuid/setgid from /bin/busybox", result.stdout)

    def test_boot_record_keeps_the_last_400_boots(self):
        log = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (log / "boots.txt").write_text("".join(f"old {n}\n" for n in range(450)))
        result = subprocess.run(["/bin/sh", str(PACKAGE / "being-boot-record")], text=True, capture_output=True,
                                timeout=10, env=dict(os.environ, BEING_LOG_DIR=str(log)))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = (log / "boots.txt").read_text().splitlines()
        self.assertEqual(len(lines), 400)
        self.assertRegex(lines[-1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ boot=")
        self.assertTrue((log / "journal").is_dir())

    def test_release_key_refuses_a_folder_in_the_repository(self):
        for folder in ("out/release", str(ROOT / "out/release-key-test")):
            result = subprocess.run(["bash", str(ROOT / "lab/build.sh"), "release-key", folder],
                                    text=True, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((ROOT / "out/release-key-test").exists())
        self.assertIn("inside the repository", result.stderr)

    def test_build_script_parses(self):
        subprocess.run(["bash", "-n", str(ROOT / "lab/build.sh")], check=True, timeout=10)
        for script in ("post-build.sh", "post-image.sh", "post-fakeroot.sh", "try-image.sh", "being-update-ready"):
            subprocess.run(["sh", "-n", str(BOARD / script)], check=True, timeout=10)
        for script in ("being-installed-generator", "being-boot-record"):
            subprocess.run(["sh", "-n", str(PACKAGE / script)], check=True, timeout=10)


def environment(sections, section="Service"):
    """Every Environment= assignment in a unit, as a dict."""
    env = {}
    for line in values(sections, section, "Environment"):
        for word in shlex.split(line):
            key, _, value = word.partition("=")
            env[key] = value
    return env


def config_options(path):
    options = {}
    for line in Path(path).read_text().splitlines():
        if match := re.match(r"^(CONFIG_\w+|BR2_\w+)=(.*)$", line):
            options[match[1]] = match[2]
        elif match := re.match(r"^# (CONFIG_\w+|BR2_\w+) is not set$", line):
            options[match[1]] = "n"
    return options


class KernelAndImageConfigTests(unittest.TestCase):
    def test_kernel_has_what_the_sandboxes_and_workshop_need(self):
        kernel = config_options(BOARD / "linux.config")
        for option in ("CONFIG_CGROUPS", "CONFIG_MEMCG", "CONFIG_CGROUP_PIDS", "CONFIG_PSI",
                       "CONFIG_BPF_SYSCALL", "CONFIG_CGROUP_BPF"):
            self.assertEqual(kernel.get(option), "y", option)
        # Defaults the laptop relies on are never overridden.
        for option in ("CONFIG_SATA_MOBILE_LPM_POLICY", "CONFIG_PSI_DEFAULT_DISABLED", "CONFIG_X86_MSR"):
            self.assertNotIn(option, kernel)
        self.assertEqual(config_options(BOARD / "linux-laptop.config").get("CONFIG_X86_MSR"), "m")

    def test_laptop_kernel_keeps_msi_the_pcie_ports_and_every_cgroup_controller(self):
        # The laptop kernel is linux.config with linux-laptop.config merged
        # over it (a later line wins). The 2012 xHCI needs MSI to find the
        # USB stick; the sandboxes need memcg, pids, PSI and cgroup BPF.
        resolved = {**config_options(BOARD / "linux.config"), **config_options(BOARD / "linux-laptop.config")}
        # CPUWeight= and CPUQuota= need the CPU controller and bandwidth
        # control, or systemd ignores them and the model shares the two
        # cores evenly with the eyes and the voice.
        for option in ("CONFIG_PCI", "CONFIG_PCI_MSI", "CONFIG_PCIEPORTBUS", "CONFIG_CGROUPS", "CONFIG_MEMCG",
                       "CONFIG_CGROUP_PIDS", "CONFIG_PSI", "CONFIG_BPF_SYSCALL", "CONFIG_CGROUP_BPF",
                       "CONFIG_CGROUP_SCHED", "CONFIG_FAIR_GROUP_SCHED", "CONFIG_CFS_BANDWIDTH"):
            self.assertEqual(resolved.get(option), "y", option)
        self.assertNotIn("CONFIG_PSI_DEFAULT_DISABLED", resolved)

    def test_both_images_ship_the_voice_engines(self):
        externals = (OS / "Config.in").read_text()
        self.assertIn('source "$BR2_EXTERNAL_BEINGOS_PATH/package/sherpa-onnx/Config.in"', externals)
        for name in ("beingos_x86_64_defconfig", "beingos_laptop_defconfig"):
            with self.subTest(defconfig=name):
                self.assertEqual(config_options(OS / "configs" / name).get("BR2_PACKAGE_SHERPA_ONNX"), "y")

    def test_every_external_package_config_exists(self):
        # Kconfig stops the whole build on a missing source file.
        for path in re.findall(r'^source "\$BR2_EXTERNAL_BEINGOS_PATH/(\S+)"$', (OS / "Config.in").read_text(), re.M):
            with self.subTest(path=path):
                self.assertTrue((OS / path).is_file(), f"os/{path} is sourced but missing")

    def test_both_images_use_libseccomp_hash_checks_and_strip_setuid(self):
        for name in ("beingos_x86_64_defconfig", "beingos_laptop_defconfig"):
            with self.subTest(defconfig=name):
                config = config_options(OS / "configs" / name)
                self.assertEqual(config.get("BR2_PACKAGE_LIBSECCOMP"), "y")
                self.assertEqual(config.get("BR2_DOWNLOAD_FORCE_CHECK_HASHES"), "y")
                self.assertEqual(config.get("BR2_ROOTFS_POST_FAKEROOT_SCRIPT"),
                                 '"$(BR2_EXTERNAL_BEINGOS_PATH)/board/beingos/post-fakeroot.sh"')
                self.assertEqual(config.get("BR2_CCACHE_INITIAL_SETUP"), '"--max-size=40G"')

    def test_laptop_defconfig_differs_from_the_lab_only_where_allowed(self):
        lab = config_options(OS / "configs/beingos_x86_64_defconfig")
        laptop = config_options(OS / "configs/beingos_laptop_defconfig")
        allowed = {
            # No way in: no getty, no root login, no SSH, no lab body, no serial GRUB.
            "BR2_TARGET_GENERIC_GETTY", "BR2_TARGET_GENERIC_GETTY_PORT", "BR2_TARGET_GENERIC_GETTY_BAUDRATE_115200",
            "BR2_TARGET_ENABLE_ROOT_LOGIN", "BR2_PACKAGE_OPENSSH", "BR2_PACKAGE_OPENSSH_CLIENT",
            "BR2_PACKAGE_OPENSSH_SERVER", "BR2_PACKAGE_OPENSSH_KEY_UTILS", "BR2_PACKAGE_BEING_LAB_BODY",
            "BR2_PACKAGE_BEING_LAN_MAINTENANCE", "BR2_TARGET_GRUB2_BUILTIN_MODULES_EFI",
            # The laptop kernel fragment and the board scripts' variant.
            "BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES", "BR2_ROOTFS_POST_SCRIPT_ARGS",
            # Build speed in the laptop tree only (the lab tree is left as it is).
            "BR2_PER_PACKAGE_DIRECTORIES", "BR2_JLEVEL",
        }
        differing = {key for key in lab.keys() | laptop.keys() if lab.get(key) != laptop.get(key)}
        self.assertLessEqual(differing, allowed, sorted(differing - allowed))
        self.assertEqual(laptop["BR2_TARGET_GRUB2_BUILTIN_MODULES_EFI"].replace('"', "").split(),
                         [m for m in lab["BR2_TARGET_GRUB2_BUILTIN_MODULES_EFI"].replace('"', "").split() if m != "serial"])

    def test_custom_downloads_have_hashes_for_the_pinned_versions(self):
        version = config_options(OS / "configs/beingos_laptop_defconfig")["BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE"]
        self.assertEqual(version, config_options(OS / "configs/beingos_x86_64_defconfig")[
            "BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE"])
        tarball = f"linux-{version.strip(chr(34))}.tar.xz"
        for path in (OS / "patches/linux/linux.hash", OS / "patches/linux-headers/linux-headers.hash"):
            self.assertRegex(path.read_text(), rf"(?m)^sha256  [0-9a-f]{{64}}  {re.escape(tarball)}$")
        cortex = re.search(r"^CORTEX_VERSION = (\S+)$", (OS / "package/cortex/cortex.mk").read_text(), re.M)[1]
        self.assertRegex((OS / "package/cortex/cortex.hash").read_text(),
                         rf"(?m)^sha256  [0-9a-f]{{64}}  {re.escape(cortex)}\.tar\.gz$")

    def test_applesmc_patch_adds_bounded_attributes_and_writes_nothing_at_probe(self):
        patch = (OS / "patches/linux/0001-hwmon-applesmc-battery-charge-limit.patch").read_text()
        self.assertEqual(set(re.findall(r"^(?:---|\+\+\+) [ab]/(\S+)", patch, re.M)), {"drivers/hwmon/applesmc.c"})
        added = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for name in ("battery_charge_limit", "battery_charge_led", "battery_charge_inhibit"):
            self.assertIn(f'"{name}"', added)
        self.assertIn("#define BATTERY_CHARGE_PERCENT_MIN\t20", added)
        self.assertIn("#define BATTERY_CHARGE_PERCENT_MAX\t100", added)
        start = added.index("static void applesmc_has_byte_key")
        probe = added[start:added.index("\n}\n", start)]
        self.assertNotIn("write", probe)
        self.assertNotIn("applesmc_write_key", added[added.index("applesmc_has_byte_key(BATTERY_CHARGE_LIMIT_KEY"):
                                                     added.index("battery charge: limit=")])


def unit(path):
    sections, current = {}, None
    text = Path(path).read_text().replace("\\\n", " ")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), [])
        else:
            key, _, value = line.partition("=")
            current.append((key, value))
    return sections


def values(sections, section, key):
    return [value for name, value in sections.get(section, []) if name == key]


class UnitTests(unittest.TestCase):
    def test_cortex_is_reachable_only_on_loopback(self):
        service = unit(OS / "package/cortex/being-cortex.service")
        self.assertEqual(values(service, "Service", "IPAddressDeny"), ["any"])
        self.assertEqual(values(service, "Service", "IPAddressAllow"), ["localhost"])
        self.assertEqual(values(service, "Service", "ProtectSystem"), ["strict"])
        self.assertEqual(values(service, "Service", "ReadWritePaths"), ["-/mind/cortex"])
        self.assertEqual(values(service, "Service", "ExecStartPre"), ["+/bin/mkdir -p /mind/cortex"])
        self.assertIn("--host 127.0.0.1", values(service, "Service", "ExecStart")[0])

    def test_organs_that_need_no_network_have_none(self):
        for name in ("being-brainstem.service", "being-voice.service", "being-eyes.service",
                     "being-bodycare.service"):
            with self.subTest(unit=name):
                service = unit(PACKAGE / name)
                self.assertEqual(values(service, "Service", "PrivateNetwork"), ["yes"])
                self.assertEqual(values(service, "Service", "NoNewPrivileges"), ["yes"])

    def test_voice_sandbox_keeps_what_the_lab_ears_need(self):
        # mind/voice.py runs "modprobe snd-aloop" in the QEMU lab and
        # mind/mic_port.py opens a /dev/vport* microphone; DevicePolicy= is
        # enforced once the kernel has cgroup BPF.
        voice = (ROOT / "mind/voice.py").read_text()
        service = unit(PACKAGE / "being-voice.service")
        if '"modprobe"' in voice:
            self.assertFalse(values(service, "Service", "ProtectKernelModules"))
        allowed = values(service, "Service", "DeviceAllow")
        self.assertIn("char-alsa rw", allowed)
        if "virtio-ports" in (ROOT / "mind/mic_port.py").read_text():
            self.assertIn("char-virtio-portsdev rw", allowed)

    def test_hands_runs_by_default_and_the_switch_lives_in_hands(self):
        # Decisions 1: on by default everywhere; hands itself honours
        # /mind/apps.disabled, so the unit must not gate on an opt-in marker.
        service = unit(PACKAGE / "being-hands.service")
        self.assertEqual(values(service, "Unit", "ConditionPathExists"), ["!/mind/.being-restore-in-progress"])
        self.assertEqual(values(service, "Unit", "ConditionKernelCommandLine"), [])
        self.assertIn('Path("/mind/apps.disabled")', (ROOT / "mind/hands.py").read_text())
        self.assertIn("/workshop", values(service, "Unit", "RequiresMountsFor"))
        self.assertEqual(values(service, "Service", "MemoryMax"), ["128M"])
        self.assertEqual(values(service, "Service", "ExecStart"), ["/usr/bin/python3 -m mind.hands --bus ${BEING_BUS}"])
        mount = unit(PACKAGE / "workshop.mount")
        options = values(mount, "Mount", "Options")[0].split(",")
        for option in ("size=64M", "nr_inodes=4096", "mode=0700", "nosuid", "nodev", "noexec",
                       "X-mount.owner=being-shell"):
            self.assertIn(option, options)
        slice_ = unit(PACKAGE / "workshop.slice")
        self.assertEqual(values(slice_, "Slice", "CPUWeight"), ["idle"])
        self.assertEqual(values(slice_, "Slice", "CPUQuota"), ["50%"])

    def test_bodycare_restores_defaults_on_any_exit(self):
        service = unit(PACKAGE / "being-bodycare.service")
        self.assertEqual(values(service, "Service", "Type"), ["notify"])
        self.assertEqual(values(service, "Service", "ExecStopPost"),
                         ["/usr/bin/being-bodycare --restore --journal /run/being-bodycare/restore.json"])
        self.assertTrue(values(service, "Service", "WatchdogSec"))
        # /sys must stay writable for the power knobs.
        self.assertFalse(values(service, "Service", "ProtectKernelTunables"))

    def test_bodycare_can_write_its_restore_journal_inside_its_sandbox(self):
        # Care::set() refuses every write it cannot journal first, so a
        # read-only journal would silently turn act mode into observe mode.
        service = unit(PACKAGE / "being-bodycare.service")
        self.assertEqual(values(service, "Service", "ProtectSystem"), ["strict"])
        writable = ["/run/" + name for line in values(service, "Service", "RuntimeDirectory")
                    for name in line.split()]
        writable += [path.lstrip("-") for line in values(service, "Service", "ReadWritePaths")
                     for path in line.split()]
        # Kept across restarts and through ExecStopPost, so the originals survive a crash.
        self.assertEqual(values(service, "Service", "RuntimeDirectoryPreserve"), ["yes"])
        for key in ("ExecStart", "ExecStopPost"):
            with self.subTest(command=key):
                words = values(service, "Service", key)[0].split()
                self.assertIn("--journal", words, "the default journal path is the brainstem's read-only /run/being")
                journal = Path(words[words.index("--journal") + 1])
                self.assertTrue(any(journal.parent == Path(place) or Path(place) in journal.parents
                                    for place in writable), (journal, writable))

    def test_bodycare_journal_lives_where_the_crate_looks_for_it(self):
        # Act mode writes nothing it has not journalled, so the unit's runtime
        # directory and the crate's own default (the first $RUNTIME_DIRECTORY)
        # must agree: a restart without --journal still finds the originals.
        service = unit(PACKAGE / "being-bodycare.service")
        self.assertEqual(values(service, "Service", "RuntimeDirectory"), ["being-bodycare"])
        self.assertEqual(values(service, "Service", "RuntimeDirectoryMode"), ["0700"])
        main = (ROOT / "crates/bodycare/src/main.rs").read_text()
        self.assertIn('std::env::var_os("RUNTIME_DIRECTORY")', main)
        self.assertIn('default_journal(Some("/run/being-bodycare".into()))', main)
        start, stop = (values(service, "Service", key)[0].split() for key in ("ExecStart", "ExecStopPost"))
        self.assertEqual(start[start.index("--journal") + 1], stop[stop.index("--journal") + 1],
                         "the restore must read the journal the running service wrote")
        # /run/being is the brainstem's; bodycare never needs it writable.
        self.assertNotIn("/run/being", " ".join(values(service, "Service", "ReadWritePaths")).split())

    def test_bench_model_server_runs_in_the_cortex_sandbox(self):
        # mind/bench.py starts llama-server as its own child, so the bench
        # unit's sandbox is the model server's: the cortex unit's, except
        # where measuring needs more.
        bench = unit(PACKAGE / "being-bench.service")
        cortex = unit(OS / "package/cortex/being-cortex.service")
        for key in ("NoNewPrivileges", "CapabilityBoundingSet", "LimitMEMLOCK", "ProtectSystem", "ProtectHome",
                    "PrivateTmp", "PrivateDevices", "IPAddressDeny", "IPAddressAllow", "RestrictAddressFamilies",
                    "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups", "ProtectClock",
                    "ProtectHostname", "LockPersonality", "RestrictRealtime", "RestrictSUIDSGID",
                    "RestrictNamespaces", "SystemCallArchitectures"):
            with self.subTest(key=key):
                self.assertTrue(values(cortex, "Service", key), key)
                self.assertEqual(values(bench, "Service", key), values(cortex, "Service", key))
        # The soak writes the CPU caps and turbo in /sys and puts them back.
        source = (ROOT / "mind/bench.py").read_text()
        self.assertIn('CPU = Path("/sys/devices/system/cpu")', source)
        self.assertFalse(values(bench, "Service", "ProtectKernelTunables"))
        # The model server itself is started with the cortex's loopback address.
        self.assertIn('"--host", "127.0.0.1"', source)

    def test_bench_can_write_its_results_inside_its_sandbox(self):
        from mind import bench, usb_logs
        service = unit(PACKAGE / "being-bench.service")
        writable = ["/run/" + name for line in values(service, "Service", "RuntimeDirectory") for name in line.split()]
        writable += [path.lstrip("-") for line in values(service, "Service", "ReadWritePaths") for path in line.split()]
        # Without a stick: /run/being-bench, kept after the oneshot has ended.
        self.assertIn("/run/being-bench", writable)
        self.assertIn('fallback = Path("/run/being-bench")', (ROOT / "mind/bench.py").read_text())
        self.assertEqual(values(service, "Service", "RuntimeDirectoryPreserve"), ["yes"])
        # With one: this boot's PIPLOGS folder, inside being-usb-logs' mount.
        self.assertIn(str(usb_logs.MOUNT_POINT), writable)
        self.assertIn("-" + str(usb_logs.MOUNT_POINT), values(service, "Service", "ReadWritePaths"),
                      "a boot without a Try stick has no PIPLOGS mount")
        self.assertEqual(bench.PIPLOGS_STATE, Path("/run/being-usb-logs/folder"))

    def test_bench_orders_the_cortex_about_from_outside_its_sandbox(self):
        service = unit(PACKAGE / "being-bench.service")
        for key, verb in (("ExecStartPre", "stop"), ("ExecStopPost", "start")):
            with self.subTest(key=key):
                (line,) = values(service, "Service", key)
                prefix = line[:line.index("/")]
                self.assertEqual(set(prefix), {"-", "+"}, line)
                self.assertIn(f" {verb} ", line)
                self.assertIn("being-cortex.service", line)
        (start,) = values(service, "Service", "ExecStart")
        self.assertTrue(start.startswith("/usr/bin/python3"), "the bench itself stays in the sandbox")

    def test_every_unit_file_here_is_installed_by_the_recipe(self):
        recipe = (PACKAGE / "being.mk").read_text()
        for path in sorted(PACKAGE.iterdir()):
            if path.suffix in (".service", ".mount", ".slice", ".target"):
                with self.subTest(unit=path.name):
                    self.assertIn(f"$(BEING_PKGDIR)/{path.name}", recipe)
        for name in ("50-being-hardening.conf", "60-being-power.rules", "50-being-journal.conf", "being-boot-record"):
            self.assertIn(f"$(BEING_PKGDIR)/{name}", recipe)

    def test_workshop_users_exist_and_are_locked(self):
        recipe = (PACKAGE / "being.mk").read_text()
        for user in ("being-shell", "being-panel"):
            self.assertEqual(len(re.findall(rf"(?m)^\t{user} -1 {user} -1 ! ", recipe)), 2, user)

    def test_hardening_sysctls(self):
        settings = dict(line.split(" = ") for line in (PACKAGE / "50-being-hardening.conf").read_text().splitlines()
                        if line and not line.startswith("#"))
        self.assertEqual(settings["user.max_user_namespaces"], "0")
        self.assertEqual(settings["kernel.unprivileged_bpf_disabled"], "1")


class OrganUnitTuningTests(unittest.TestCase):
    """Wave 2 unit tuning (docs/PIP-FUNCTIONS.md, L10.4 and L10.5)."""

    def test_the_mind_never_waits_for_the_model(self):
        mind = unit(PACKAGE / "being-mind.service")
        after = " ".join(values(mind, "Unit", "After")).split()
        self.assertIn("being-brainstem.service", after)
        self.assertNotIn("being-cortex.service", after)
        for key in ("Requires", "Wants", "BindsTo", "Requisite"):
            self.assertNotIn("being-cortex.service", " ".join(values(mind, "Unit", key)), key)

    def test_face_voice_brainstem_and_mind_come_first_for_the_cpu(self):
        for name in ("being-face.service", "being-mind.service", "being-brainstem.service"):
            with self.subTest(unit=name):
                self.assertEqual(values(unit(PACKAGE / name), "Service", "CPUWeight"), ["1000"])

    def test_python_organs_are_free_to_use_their_compiled_modules(self):
        for name in ("being-mind.service", "being-face.service", "being-local-screen.service"):
            with self.subTest(unit=name):
                self.assertNotIn("PYTHONDONTWRITEBYTECODE", environment(unit(PACKAGE / name)))

    def test_face_has_a_writable_shader_cache_that_survives_its_restarts(self):
        face = unit(PACKAGE / "being-face.service")
        env = environment(face)
        self.assertEqual(env["MESA_SHADER_CACHE_DIR"], "/run/being/mesa")
        self.assertTrue(env["MESA_SHADER_CACHE_MAX_SIZE"].endswith("M"))
        self.assertEqual(values(face, "Service", "RuntimeDirectory"), ["being/mesa"])
        self.assertEqual(values(face, "Service", "RuntimeDirectoryPreserve"), ["yes"])
        # The brainstem owns /run/being itself and keeps it across restarts.
        stem = unit(PACKAGE / "being-brainstem.service")
        self.assertEqual(values(stem, "Service", "RuntimeDirectory"), ["being"])
        self.assertEqual(values(stem, "Service", "RuntimeDirectoryPreserve"), ["yes"])
        # The Try menu (being-console) shares it; the first boot logged
        # "Failed to create /root/.cache for shader cache" from it.
        self.assertEqual(environment(unit(PACKAGE / "being-local-screen.service"))["MESA_SHADER_CACHE_DIR"],
                         "/run/being/mesa")

    def test_face_is_watched_and_its_frame_loop_kicks_the_watchdog(self):
        face = unit(PACKAGE / "being-face.service")
        self.assertEqual(values(face, "Service", "WatchdogSec"), ["5s"])
        self.assertEqual(values(face, "Service", "NotifyAccess"), ["main"])
        self.assertEqual(values(face, "Service", "Restart"), ["always"])
        source = (ROOT / "crates/face/src/main.rs").read_text()
        self.assertIn("bus::notify::Notifier::from_env()", source)
        loop = source.index("'run: loop {")
        self.assertLess(loop, source.index("watchdog.kick();", loop), "kicked inside the frame loop")
        # The slowest frame rate (a dark panel) still kicks well inside 5 s.
        self.assertIn("Activity::Blank => 5,", (ROOT / "crates/face/src/pace.rs").read_text())
        # A long verbose boot wait kicks too.
        self.assertIn('hold_for_boot_messages("face", &mut || watchdog.kick())', source)

    def test_the_mind_is_watched_only_once_it_pings(self):
        mind = unit(PACKAGE / "being-mind.service")
        if values(mind, "Service", "WatchdogSec"):
            self.assertTrue((ROOT / "mind/sd_notify.py").exists(), "a watchdog with nothing to ping it")
            self.assertIn("sd_notify.keep_alive(", (ROOT / "mind/__main__.py").read_text())
            self.assertEqual(values(mind, "Service", "NotifyAccess"), ["main"])
            self.assertEqual(values(mind, "Service", "WatchdogSec"), ["30s"])
            # Pings come well inside the watchdog.
            from mind import sd_notify
            self.assertLessEqual(sd_notify.PING_SECONDS * 2, 30)


BRAINSTEM = ROOT / "target/debug/brainstem"


@unittest.skipUnless(BRAINSTEM.exists(), "brainstem is not built: run `cargo build -p brainstem`")
class MindWithoutCortexTests(unittest.IsolatedAsyncioTestCase):
    """The mind starts before the model (no After=being-cortex.service), so
    with no model server at all it must still answer what code answers."""

    async def test_a_mind_with_no_cortex_still_tells_the_time(self):
        import asyncio
        import io
        import socket
        from mind.bus import Bus
        from sim.body import SUBS, SimBody
        from tests.test_end_to_end import Watcher

        work = self.enterContext(tempfile.TemporaryDirectory(prefix="bnc-", dir="/tmp"))
        bus_path = os.path.join(work, "bus.sock")
        # A port nothing listens on: the cortex is not there at all.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        procs = []

        def spawn(argv, **kwargs):
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
            procs.append(proc)
            return proc

        def stop_all():
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
            for proc in procs:
                proc.wait(timeout=5)

        self.addCleanup(stop_all)
        spawn([str(BRAINSTEM), "--bus", bus_path, "--require", "mind,voice"])
        for _ in range(250):
            if os.path.exists(bus_path):
                break
            await asyncio.sleep(0.02)
        watcher = Watcher(bus_path)
        await watcher.start()
        self.addAsyncCleanup(watcher.stop)
        spawn([sys.executable, "-m", "mind", "--bus", bus_path, "--cortex", f"http://127.0.0.1:{closed}",
               "--mind-dir", os.path.join(work, "mind")], cwd=ROOT)
        heard = io.StringIO()
        lines = asyncio.Queue()

        async def typed():
            while (line := await lines.get()) is not None:
                yield line

        body = SimBody(Bus("voice", SUBS, path=bus_path), "Richard", silent=True, out=heard)
        body_task = asyncio.create_task(body.run(typed()))

        async def finish():
            lines.put_nowait(None)
            await asyncio.wait_for(body_task, 5)

        self.addAsyncCleanup(finish)
        await watcher.until("the mind", lambda e: e["t"] == "mind.ready", timeout=15)
        await watcher.state("idle")
        lines.put_nowait("What time is it?")
        await watcher.until("the answer", lambda e: e["t"] == "say.done" and e.get("final"), timeout=15)
        said = " ".join(line.strip() for line in heard.getvalue().splitlines()
                        if line.strip() and not line.startswith("("))
        self.assertRegex(said, r"It's (\w+|\d)")


class ImageRecipeTests(unittest.TestCase):
    """What being.mk puts in the image beside the organs (L10.2, L10.3)."""

    recipe = (PACKAGE / "being.mk").read_text()

    def define(self, name):
        match = re.search(rf"^define {name}\n(.*?)^endef$", self.recipe, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match[1]

    def test_audio_init_is_installed_once_its_files_exist(self):
        self.assertIn("$(BEING_PKGDIR)/being-audio-init $(TARGET_DIR)/usr/libexec/being/being-audio-init",
                      self.define("BEING_INSTALL_AUDIO_INIT"))
        self.assertIn("system/being-audio-init.service", self.define("BEING_INSTALL_AUDIO_INIT_UNIT"))
        self.assertIn("$(BEING_INSTALL_AUDIO_INIT)", self.define("BEING_INSTALL_TARGET_CMDS"))
        self.assertIn("$(BEING_INSTALL_AUDIO_INIT_UNIT)", self.define("BEING_INSTALL_INIT_SYSTEMD"))
        for name in ("being-audio-init", "being-audio-init.service"):
            self.assertIn(f"package/being/{name})", self.recipe.split("BEING_HAS_AUDIO_INIT =", 1)[1].split("\n\n", 1)[0])
        service = PACKAGE / "being-audio-init.service"
        if service.exists():
            sections = unit(service)
            self.assertEqual(values(sections, "Service", "ExecStart")[0].lstrip("-+!@").split()[0],
                             "/usr/libexec/being/being-audio-init")
            self.assertIn("being-voice.service", " ".join(values(sections, "Unit", "Before")))

    def test_bench_unit_is_installed_and_started_only_by_a_measuring_boot(self):
        self.assertIn("system/being-bench.service", self.define("BEING_INSTALL_BENCH_UNIT"))
        self.assertIn("$(BEING_INSTALL_BENCH_UNIT)", self.define("BEING_INSTALL_INIT_SYSTEMD"))
        self.assertIn("package/being/being-bench.service)", self.recipe)

    def test_every_model_pack_descriptor_is_shipped_where_model_pack_reads_it(self):
        packs = self.define("BEING_INSTALL_MODEL_PACKS")
        self.assertIn("$(BEING_PKGDIR)/model-packs/*/pack.json", packs)
        self.assertIn("$(TARGET_DIR)/usr/share/being/model-packs/$$role/pack.json", packs)
        self.assertIn("$(BEING_INSTALL_MODEL_PACKS)", self.define("BEING_INSTALL_TARGET_CMDS"))
        roles = {path.parent.name for path in (PACKAGE / "model-packs").glob("*/pack.json")}
        self.assertTrue({"dream", "vad", "asr", "tts", "embeddings", "speaker"} <= roles, roles)
        # The shell loop, run as make would after expanding $$ to $.
        target = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = (packs.replace("$(TARGET_DIR)", str(target)).replace("$(BEING_PKGDIR)", str(PACKAGE))
                  .replace("$(INSTALL)", "install").replace("$$", "$").replace("\\\n", " "))
        # A portable stand-in for GNU `install -D -m MODE SRC DST`.
        shim = 'install() { shift 3; mkdir -p "$(dirname "$2")"; cp "$1" "$2"; }\n'
        subprocess.run(["sh", "-ec", shim + script.replace("\t", "")], check=True)
        for role in roles:
            self.assertEqual((target / "usr/share/being/model-packs" / role / "pack.json").read_bytes(),
                             (PACKAGE / "model-packs" / role / "pack.json").read_bytes())

    def test_python_ships_compiled_with_unchecked_hashes(self):
        self.assertIn("host-python3", re.search(r"^BEING_DEPENDENCIES = (.*)$", self.recipe, re.M)[1].split())
        compile_ = self.define("BEING_COMPILE_PYTHON")
        self.assertIn("$(HOST_DIR)/bin/python3 -m compileall", compile_)
        self.assertIn("--invalidation-mode unchecked-hash", compile_)
        self.assertIn("-d /usr/lib/being $(TARGET_DIR)/usr/lib/being", compile_)
        install = self.define("BEING_INSTALL_TARGET_CMDS")
        # After everything Python is in place, so nothing is left uncompiled.
        self.assertGreater(install.index("$(BEING_COMPILE_PYTHON)"), install.index("$(@D)/mind/*.py"))
        self.assertGreater(install.index("$(BEING_COMPILE_PYTHON)"), install.index("$(BEING_INSTALL_LAB_BODY)"))

    def test_every_mind_module_compiles_the_way_the_image_compiles_it(self):
        target = Path(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.copytree(ROOT / "mind", target / "usr/lib/being/mind",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "workshop_shelf"))
        result = subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", "--invalidation-mode",
                                 "unchecked-hash", "-d", "/usr/lib/being", str(target / "usr/lib/being")],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compiled = list((target / "usr/lib/being/mind/__pycache__").glob("turn.*.pyc"))
        self.assertEqual(len(compiled), 1)
        flags = int.from_bytes(compiled[0].read_bytes()[4:8], "little")
        self.assertEqual(flags, 0b01, "hash-based and unchecked (PEP 552)")

    def test_preset_keeps_the_night_and_bench_units_off_and_the_organs_on(self):
        rules = [line.split() for line in (PACKAGE / "50-being.preset").read_text().splitlines()
                 if line.strip() and not line.startswith("#")]

        def verdict(name):
            # systemd uses the first line whose pattern matches.
            import fnmatch
            return next((action for action, pattern in rules if fnmatch.fnmatchcase(name, pattern)), None)

        self.assertEqual(verdict("being-dream-cortex.service"), "disable")
        self.assertEqual(verdict("being-bench.service"), "disable")
        self.assertEqual(verdict("being-lab-dev.service"), "disable")
        for name in ("being-mind.service", "being-face.service", "being-voice.service", "being-audio-init.service"):
            self.assertEqual(verdict(name), "enable", name)


class BenchBootTests(unittest.TestCase):
    GUID = "aec08b06-4dd2-4b82-88a0-2822192e249c"

    def generate(self, command_line):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (work / "cmdline").write_text(command_line + "\n")
        dirs = [work / name for name in ("normal", "early", "late")]
        for directory in dirs:
            directory.mkdir()
        script = (PACKAGE / "being-try-generator").read_text().replace("/proc/cmdline", shlex.quote(str(work / "cmdline")))
        subprocess.run(["/bin/sh", "-c", script, "gen", *map(str, dirs)], check=True, capture_output=True, timeout=5)
        return dirs[1] / "multi-user.target.wants/being-bench.service"

    def test_only_a_try_boot_with_bench_on_starts_bench_mode(self):
        link = self.generate(f"being.try=1 being.models={self.GUID} being.bench=1")
        self.assertEqual(os.readlink(link), "/usr/lib/systemd/system/being-bench.service")
        for line in (f"being.try=1 being.models={self.GUID}", f"being.try=1 being.models={self.GUID} being.bench=0",
                     f"being.bench=1 being.models={self.GUID}"):
            with self.subTest(cmdline=line):
                self.assertFalse(os.path.lexists(self.generate(line)))


class PowerRuleTests(unittest.TestCase):
    def test_keyboard_and_trackpad_are_never_autosuspended(self):
        rules = [line for line in (PACKAGE / "60-being-power.rules").read_text().splitlines()
                 if line and not line.startswith("#")]
        for line in rules:
            if "0252" in line or "0253" in line or "0254" in line:
                self.assertIn('ATTR{power/control}="on"', line)
                self.assertNotIn('"auto"', line)
        autos = [line for line in rules if 'ATTR{power/control}="auto"' in line]
        products = {re.search(r'ATTR\{idProduct\}=="([^"]+)"', line)[1] for line in autos}
        self.assertEqual(products, {"8509", "821d|828c", "8242"})
        for line in rules:
            self.assertNotIn("link_power_management_policy", line)


# A stand-in docker for lab/build.sh and lab/make-iso.sh. It logs every call,
# runs no make (and no Buildroot download), and on the host, with /src and
# /work mapped into the scratch tree, runs only the steps that put images
# and their source ids in place.
BUILD_FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["FAKE_ROOT"]
args = sys.argv[1:]
with open(os.path.join(root, "docker-calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] == ["--context"]:
    args = args[2:]
if args[:2] == ["image", "inspect"]:
    print("sha256:fixture")
    sys.exit(0)
if args[:1] != ["run"]:
    sys.exit(0)
args = args[1:]
env = {}
while args and args[0] not in ("beingos-builder", "pipos-iso-tools"):
    option = args.pop(0)
    if option == "-e":
        key, _, value = args.pop(0).partition("=")
        env[key] = value
    elif option == "-v":
        args.pop(0)
image, command = args[0], args[1:]
def mapped(part):
    return part.replace("/src/", root + "/").replace("/work/", root + "/work/").replace("--sparse=always ", "")
if image == "pipos-iso-tools":
    # make-iso.sh's inner script: an ISO, and the id it found beside the images.
    sys.stdin.read()
    out, name = mapped(env["OUT"] + "/"), env["NAME"]
    with open(out + name + ".iso", "w") as iso:
        iso.write("iso fixture\n")
    with open(out + name + ".iso.sha256", "w") as digest:
        digest.write("fixture  " + name + ".iso\n")
    record = mapped(env["IMAGES"] + "/") + "being-source-id"
    found = open(record).read().strip() if os.path.exists(record) else ""
    with open(out + "." + name + ".iso.built-from", "w") as built:
        built.write(found + "\n")
    sys.exit(0)
if command[:1] == ["make"]:
    targets = [part for part in command[1:] if not part.startswith(("-", "O=", "BR2_EXTERNAL="))]
    with open(os.path.join(root, "make-targets.jsonl"), "a") as log:
        log.write(json.dumps(targets) + "\n")
    # The tree changes while the build runs.
    touch = os.environ.get("FAKE_TOUCH_DURING_MAKE")
    if touch:
        with open(os.path.join(root, touch), "a") as changed:
            changed.write("# changed during the build\n")
    sys.exit(0)
runs = ("try-image.sh", "cp --sparse", "record-source-id")
if not (any(any(word in part for word in runs) for part in command)
        or (command[:2] == ["rm", "-f"] and command[2].endswith("/being-source-id"))):
    sys.exit(0)
command = [mapped(part) for part in command]
child = {key: value for key, value in os.environ.items() if not key.startswith("BEING_TRY_")}
child.update(env)
os.execvpe(command[0], command, child)
'''


class BuildScriptSourceIdTests(unittest.TestCase):
    """lab/build.sh and lab/make-iso.sh, run for real against stand-in colima and docker."""

    TREES = {"build": "x86_64", "try-image": "x86_64", "try-screen": "x86_64",
             "laptop": "laptop", "laptop-try": "laptop"}
    IMAGES = {"build": "beingos.img", "try-image": "beingos-try.img", "try-screen": "beingos-try-screen.img",
              "laptop": "beingos-laptop.img", "laptop-try": "beingos-laptop-try.img"}

    def setUp(self):
        from tests.test_build_try_bench import try_image_options
        from lab import candidate_manifest
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = scratch / "tree"
        (self.root / "lab").mkdir(parents=True)
        for name in ("build.sh", "make-iso.sh", "source_id.py", "candidate_manifest.py", "__init__.py"):
            shutil.copy2(ROOT / "lab" / name, self.root / "lab" / name)
        for scope in candidate_manifest.IMAGE_SCOPES:
            path = self.root / scope
            if scope.startswith("Cargo."):
                path.write_text(scope + "\n")
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / "fixture").write_text(scope + "\n")
        board = self.root / "os/board/beingos"
        board.mkdir(parents=True)
        (board / "try-image.sh").write_text(try_image_options())
        for tree in ("x86_64", "laptop", "laptop-maintenance"):
            images = self.root / "work/output" / tree / "images"
            (images / "efi-part/EFI/BOOT").mkdir(parents=True)
            (self.root / "work/output" / tree / "host").mkdir()
            for name in ("efi-part/EFI/BOOT/bootx64.efi", "rootfs.squashfs", "models.ext4", "beingos.img"):
                (images / name).write_text(f"{tree} fixture\n")
        commands = scratch / "bin"
        commands.mkdir()
        (commands / "colima").write_text("#!/bin/sh\nexit 0\n")
        (commands / "docker").write_text(BUILD_FAKE_DOCKER)
        for command in commands.iterdir():
            command.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("BEING_")}
        self.env.update(PATH=str(commands) + os.pathsep + os.environ["PATH"], FAKE_ROOT=str(self.root),
                        PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("PYTHONPATH", None)

    def run_script(self, *argv, **env):
        return subprocess.run(["bash", *argv], cwd=self.root, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def tree_id(self):
        result = subprocess.run([sys.executable, "-m", "lab.source_id"], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=30, check=True)
        return result.stdout.strip()

    def sidecar(self, name, folder="out/images"):
        path = self.root / folder / f"{name}.source-id"
        return path.read_text().strip() if path.exists() else None

    def record(self, tree):
        path = self.root / "work/output" / tree / "images/being-source-id"
        return path.read_text().strip() if path.exists() else None

    def make_targets(self):
        log = self.root / "make-targets.jsonl"
        targets = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if log.exists():
            log.unlink()
        return targets

    # Each command as it rebuilds the being package and then the image.
    FRESH_ARGV = {"build": ("build", "being-rebuild", "all")}

    def test_every_image_command_records_the_id_taken_before_the_build(self):
        for command, image in self.IMAGES.items():
            with self.subTest(command=command):
                before = self.tree_id()
                # The tree changes while make runs: the image keeps the id it started from.
                argv = self.FRESH_ARGV.get(command, (command,))
                result = self.run_script("lab/build.sh", *argv, FAKE_TOUCH_DURING_MAKE=f"mind/{command}.py")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((self.root / "out/images" / image).exists())
                self.assertNotEqual(self.tree_id(), before)
                self.assertEqual(self.sidecar(image), before)
                self.assertEqual(self.record(self.TREES[command]), before)
                self.make_targets()

    def test_the_gate_accepts_a_freshly_built_image(self):
        from lab import source_id
        self.assertEqual(self.run_script("lab/build.sh", "laptop-try").returncode, 0)
        image = self.root / "out/images/beingos-laptop-try.img"
        status, message = source_id.check(image, self.root)
        source_id.cached_tree_id.cache_clear()
        self.assertEqual(status, "match", message)

    def test_a_new_image_never_keeps_an_old_images_id(self):
        old = "0" * 64
        images = self.root / "out/images"
        images.mkdir(parents=True)
        (images / "beingos-try.img").write_text("old image\n")
        (images / "beingos-try.img.source-id").write_text(old + "\n")
        (self.root / "lab/source_id.py").unlink()
        result = self.run_script("lab/build.sh", "try-image")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gave no source id", result.stderr)
        self.assertIsNone(self.sidecar("beingos-try.img"))
        self.assertIsNone(self.record("x86_64"))

    def test_the_being_package_is_cleaned_before_every_rebuild(self):
        for command in ("try-image", "try-screen", "laptop", "laptop-try"):
            with self.subTest(command=command):
                self.assertEqual(self.run_script("lab/build.sh", command).returncode, 0)
                targets = [target for call in self.make_targets() for target in call]
                self.assertIn("being-dirclean", targets)
                self.assertLess(targets.index("being-dirclean"), targets.index("being-rebuild"))
                self.assertLess(targets.index("being-rebuild"), targets.index("all"))
        for argv in (("build", "being-rebuild", "all"), ("laptop", "being-rebuild", "all")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_script("lab/build.sh", *argv).returncode, 0)
                targets = [target for call in self.make_targets() for target in call]
                self.assertLess(targets.index("being-dirclean"), targets.index("being-rebuild"))

    def test_an_image_whose_being_package_was_not_rebuilt_gets_no_id(self):
        # Buildroot never copies the local being source in again by itself:
        # a plain "make all" can assemble an image from code copied in long
        # ago, so naming the tree's id beside it would let the gate pass it.
        images = self.root / "out/images"
        images.mkdir(parents=True)
        for argv, image, tree in ((("build",), "beingos.img", "x86_64"),
                                  (("build", "linux-rebuild", "all"), "beingos.img", "x86_64"),
                                  (("build", "being-rebuild"), "beingos.img", "x86_64"),
                                  (("laptop", "linux-rebuild", "all"), "beingos-laptop.img", "laptop"),
                                  (("laptop", "all", "being-rebuild"), "beingos-laptop.img", "laptop")):
            with self.subTest(argv=argv):
                (images / f"{image}.source-id").write_text("0" * 64 + "\n")
                (self.root / "work/output" / tree / "images/being-source-id").write_text("0" * 64 + "\n")
                result = self.run_script("lab/build.sh", *argv)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNone(self.sidecar(image))
                self.assertIsNone(self.record(tree))
                self.assertIn("did not rebuild the being package", result.stderr)
                self.make_targets()

    def test_being_rebuild_runs_on_its_own_between_the_other_targets(self):
        # With per-package directories a parallel "being-rebuild all" can
        # assemble the image before the rebuild has installed.
        result = self.run_script("lab/build.sh", "laptop", "grub2-rebuild", "linux-reconfigure", "being-rebuild", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [[target for target in call if target != "/work/buildroot"] for call in self.make_targets()]
        tail = calls[calls.index(["grub2-rebuild", "linux-reconfigure"]):]
        self.assertEqual(tail, [["grub2-rebuild", "linux-reconfigure"], ["being-dirclean"], ["being-rebuild"], ["all"]])
        self.assertEqual(self.sidecar("beingos-laptop.img"), self.tree_id())

    def test_an_explicit_dirclean_before_all_counts_as_a_fresh_build(self):
        result = self.run_script("lab/build.sh", "build", "being-dirclean", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sidecar("beingos.img"), self.tree_id())

    def test_a_fast_rebuild_is_recorded_with_a_warning(self):
        result = self.run_script("lab/build.sh", "try-image", BEING_FAST_REBUILD="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sidecar("beingos-try.img"), self.tree_id())
        self.assertIn("may have kept older files", result.stderr)

    def test_fast_rebuild_and_plain_builds_skip_the_clean(self):
        for argv, env in ((("try-image",), {"BEING_FAST_REBUILD": "1"}),
                          (("laptop-try",), {"BEING_FAST_REBUILD": "1"}),
                          (("build",), {}), (("laptop", "linux-rebuild", "all"), {})):
            with self.subTest(argv=argv, env=env):
                result = self.run_script("lab/build.sh", *argv, **env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                targets = [target for call in self.make_targets() for target in call]
                self.assertNotIn("being-dirclean", targets)
        (self.root / "docker-calls.jsonl").unlink()
        result = self.run_script("lab/build.sh", "try-image", BEING_FAST_REBUILD="yes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("BEING_FAST_REBUILD must be 0 or 1", result.stderr)
        self.assertFalse((self.root / "docker-calls.jsonl").exists(), "refused before anything started")

    def test_a_hand_run_make_forgets_the_builds_source_id(self):
        self.assertEqual(self.run_script("lab/build.sh", "laptop").returncode, 0)
        self.assertIsNotNone(self.record("laptop"))
        self.assertEqual(self.run_script("lab/build.sh", "laptop-make", "being-rebuild").returncode, 0)
        self.assertIsNone(self.record("laptop"))

    def test_iso_records_the_source_of_the_laptop_build_it_holds(self):
        self.assertEqual(self.run_script("lab/build.sh", "laptop").returncode, 0)
        fresh = self.tree_id()
        result = self.run_script("lab/make-iso.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sidecar("pipos-macbook.iso", "out/iso"), fresh)
        self.assertFalse((self.root / "out/iso/.pipos-macbook.iso.built-from").exists())
        # The tree moves on: the ISO still names the build it holds, and says so.
        (self.root / "mind/later.py").write_text("later\n")
        result = self.run_script("lab/make-iso.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sidecar("pipos-macbook.iso", "out/iso"), fresh)
        self.assertIn("rebuild with lab/build.sh laptop", result.stderr)
        # A build with no record (an older build, or a hand-run make): no id at all.
        (self.root / "work/output/laptop/images/being-source-id").unlink()
        result = self.run_script("lab/make-iso.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.sidecar("pipos-macbook.iso", "out/iso"))

    def test_iso_reads_the_record_build_sh_writes(self):
        build = (ROOT / "lab/build.sh").read_text()
        self.assertIn("BUILD_SOURCE_ID=being-source-id", build)
        self.assertIn('"$IMAGES/being-source-id"', (ROOT / "lab/make-iso.sh").read_text())

    def test_iso_script_parses(self):
        subprocess.run(["bash", "-n", str(ROOT / "lab/make-iso.sh")], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
