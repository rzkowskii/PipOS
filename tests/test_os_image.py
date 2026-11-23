"""Host checks for the os-image lane: boot configuration, update identity,
kernel and image settings, the organ units and the board scripts.

These run on the Mac without Buildroot. Anything that needs the builder, a VM
or the laptop is named in the plan, not faked here.
"""

import os
import re
import shlex
import shutil
import stat
import subprocess
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

    def test_every_laptop_kernel_line_sends_function_keys_and_keeps_msr_read_only(self):
        installed = render(BOARD / "grub.cfg", "laptop")
        for mode, args in extra_args(installed).items():
            with self.subTest(mode=mode):
                self.assertIn("hid_apple.fnmode=2", args.split())
                self.assertIn("msr.allow_writes=off", args.split())
        try_config = render(BOARD / "try-grub.cfg", "laptop")
        lines = kernel_lines(try_config)
        self.assertEqual(len(lines), 3, lines)
        for line in lines:
            self.assertIn("hid_apple.fnmode=2", line)

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
        self.assertEqual(values(service, "Service", "ExecStopPost"), ["/usr/bin/being-bodycare --restore"])
        self.assertTrue(values(service, "Service", "WatchdogSec"))
        # /sys must stay writable for the power knobs.
        self.assertFalse(values(service, "Service", "ProtectKernelTunables"))

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


if __name__ == "__main__":
    unittest.main()
