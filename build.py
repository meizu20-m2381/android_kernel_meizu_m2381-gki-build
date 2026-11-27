#!/usr/bin/env python3
"""
Decoupled module builder for Meizu 20 (M2381) on official GKI 6.18 or ACK 6.18-lts.
Zero hardcoded machine paths; zero dependency on private kernel trees.
"""
import argparse
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request


GKI_SIGNED_MODULES = {
    "bluetooth.ko", "btbcm.ko", "btqca.ko", "btsdio.ko", "cctrng.ko",
    "dwc_pcie_pmu.ko", "hci_uart.ko", "hidp.ko", "pwrseq-core.ko",
    "rfcomm.ko", "rfkill.ko", "tipc.ko", "tipc_diag.ko",
    "virtio_console.ko", "zram.ko", "zsmalloc.ko",
}


def load_module_macros(conf_path: pathlib.Path) -> str:
    defs = []
    for line in conf_path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            defs.append(s)
    return "KCFLAGS=-Wno-unused-but-set-global " + " ".join(defs)


def fetch_gki_artifacts(build_id: str, dest: pathlib.Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    base = f"https://ci.android.com/builds/submitted/{build_id}/kernel_aarch64/latest/raw/"
    for name in [
        "gki-info.txt",
        "vmlinux.symvers",
        f"manifest_{build_id}.xml",
        "Image",
        "system_dlkm_staging_archive.tar.gz",
    ]:
        out = dest / name
        if out.exists():
            print(f"[fetch] cached: {out}")
            continue
        print(f"[fetch] downloading {base}{name} ...")
        req = urllib.request.Request(base + name, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out.write_bytes(r.read())

    # Extract the 16 GKI-protected signed modules for this exact build ID
    signed_dir = dest / "signed-sysdlkm"
    signed_dir.mkdir(exist_ok=True)
    with tarfile.open(dest / "system_dlkm_staging_archive.tar.gz", "r:gz") as tf:
        for member in tf.getmembers():
            base_name = os.path.basename(member.name)
            if base_name in GKI_SIGNED_MODULES and member.isfile():
                data = tf.extractfile(member).read()
                (signed_dir / base_name).write_bytes(data)
    print(f"[fetch] extracted {len(list(signed_dir.glob('*.ko')))} signed GKI modules into {signed_dir}")
    # Also extract exact __crc_* exported symbols from signed GKI system_dlkm modules
    readelf = shutil.which("llvm-readelf") or shutil.which("readelf")
    if readelf:
        sv_lines = set()
        for ko in sorted(signed_dir.glob("*.ko")):
            modname = ko.stem.replace("-", "_")
            secs_out = subprocess.check_output([readelf, "-S", "-W", str(ko)], text=True)
            sec_idx, sec_info = {}, {}
            for l in secs_out.splitlines():
                l = l.strip()
                if not l.startswith("["):
                    continue
                parts = l.replace("[", "").replace("]", " ").split()
                if len(parts) >= 6 and parts[0].isdigit():
                    sec_idx[int(parts[0])] = parts[1]
                    sec_info[parts[1]] = (int(parts[4], 16), int(parts[5], 16))
            raw = ko.read_bytes()
            syms_out = subprocess.check_output([readelf, "-s", "-W", str(ko)], text=True)
            for l in syms_out.splitlines():
                parts = l.split()
                if len(parts) >= 8 and parts[7].startswith("__crc_"):
                    sym, val, ndx = parts[7][6:], int(parts[1], 16), parts[6]
                    if ndx.isdigit():
                        sname = sec_idx.get(int(ndx), "")
                        if sname in sec_info:
                            crc, = struct.unpack_from("<I", raw, sec_info[sname][0] + val)
                            etype = "EXPORT_SYMBOL_GPL" if "gpl" in sname else "EXPORT_SYMBOL"
                            sv_lines.add(f"0x{crc:08x}\t{sym}\t{modname}\t{etype}\t")
                    elif ndx == "ABS":
                        sv_lines.add(f"0x{val:08x}\t{sym}\t{modname}\tEXPORT_SYMBOL\t")
        (dest / "gki-sysdlkm.symvers").write_text("\n".join(sorted(sv_lines)) + "\n")
        print(f"[fetch] extracted {len(sv_lines)} official system_dlkm CRCs -> {dest / 'gki-sysdlkm.symvers'}")


def build_outtree(args) -> None:
    ack = pathlib.Path(args.kernel_src).resolve()
    kout = pathlib.Path(args.kernel_out).resolve()
    ext = pathlib.Path(args.modules_src).resolve()
    stage = pathlib.Path(args.stage).resolve()
    vinc = ext / "include/vendor"
    conf = ext / "configs/m2381_modules.conf"
    if not vinc.is_dir() or not conf.is_file():
        sys.exit(f"ERROR: expected {vinc} and {conf} inside --modules-src")

    all_sv = kout / "m2381-outtree-all.symvers"
    by_sym = {}
    for raw_sv in args.extra_symvers.split(","):
        raw_sv = raw_sv.strip()
        if not raw_sv:
            continue
        svp = pathlib.Path(raw_sv).resolve()
        if svp.exists():
            for line in svp.read_text().splitlines():
                if "\tvmlinux\t" in line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    by_sym[parts[1]] = line
    all_sv.write_text("\n".join(by_sym.values()) + ("\n" if by_sym else ""))

    env = os.environ.copy()
    if args.clang_bin:
        env["PATH"] = str(pathlib.Path(args.clang_bin).resolve()) + ":" + env["PATH"]

    kcflags_str = load_module_macros(conf)
    base_make = [
        "make", "-C", str(ack), f"O={kout}",
        "ARCH=arm64", "LLVM=1", "LLVM_IAS=1", "CROSS_COMPILE=aarch64-linux-gnu-",
        f"NOSTDINC_FLAGS=-nostdinc -I{vinc} -I{vinc}/uapi",
        "HOSTCFLAGS=-Wno-unused-but-set-global",
        kcflags_str,
        f"-j{args.jobs}",
    ]

    def extbuild(rel_path: str, extra_args=None):
        m_dir = ext / rel_path
        mo_dir = kout / "m2381-external" / rel_path
        mo_dir.mkdir(parents=True, exist_ok=True)
        cmd = base_make + [
            f"M={m_dir}", f"MO={mo_dir}", f"KBUILD_EXTRA_SYMBOLS={all_sv}",
        ]
        if extra_args:
            cmd.extend(extra_args)
        print(f"==> [OUTTREE] {rel_path}", flush=True)
        r = subprocess.run(cmd + ["modules"], env=env)
        if r.returncode != 0:
            sys.exit(r.returncode)
        inst = [
            f"INSTALL_MOD_PATH={stage}", "INSTALL_MOD_STRIP=1", "INSTALL_MOD_DIR=extra",
            "modules_install",
        ]
        r2 = subprocess.run(cmd + inst, env=env)
        if r2.returncode != 0:
            sys.exit(r2.returncode)
        sv = mo_dir / "Module.symvers"
        if sv.exists() and sv.stat().st_size > 0:
            by_sym = {}
            for line in all_sv.read_text().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    by_sym[parts[1]] = line
            for line in sv.read_text().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    by_sym[parts[1]] = line
            all_sv.write_text("\n".join(by_sym.values()) + "\n")

    if args.only:
        only_set = set(args.only.split(","))
    else:
        only_set = None

    def maybe(name, rel_path, extra_args=None):
        if only_set is None or name in only_set or rel_path in only_set:
            extbuild(rel_path, extra_args)

    mm = "qcom/opensource/mm-drivers"
    secure = "qcom/opensource/securemsm-kernel"
    touch = "qcom/opensource/touch-drivers"
    display = "qcom/opensource/display-drivers"
    gpu = "qcom/opensource/graphics-kernel"
    audio = "qcom/opensource/audio-kernel"
    ipa = "qcom/opensource/dataipa"
    camera = "qcom/opensource/camera-kernel"
    video = "qcom/opensource/video-driver"
    eva = "qcom/opensource/eva-kernel"
    wlan = "qcom/opensource/wlan/platform"
    wifi = "qcom/opensource/wlan"
    cld = f"{wifi}/qcacld-3.0"
    bt = "qcom/opensource/bt-kernel"
    rmnet = "qcom/opensource/datarmnet/core"

    for p in [
        "meizu/system", f"{touch}/mz_gesture", "meizu/power-notify", "meizu/storage",
        "meizu/bootloader-lock", "meizu/bootloader-log", "meizu/key-value", "meizu/reset-info",
        "meizu/tof-ai11ba", "meizu/tof-vl53l1", "meizu/v4l2loopback", "meizu/pwm-ir",
        "meizu/audio-dummy", "meizu/cs35l45", "meizu/cirrus-calibration",
        "meizu/power-logger", "meizu/power-thermal", "meizu/power-interface",
        "meizu/power-battery-logger", "meizu/power-manager", "meizu/power-charger",
        "qcom/opensource/pmic-haptics",
    ]:
        maybe(p, p)

    maybe("touch", touch, [
        f"TOUCH_ROOT={ext}/{touch}", "CONFIG_ARCH_KALAMA=y", "CONFIG_MSM_TOUCH=m",
        "CONFIG_QTS_ENABLE=y", "CONFIG_TOUCHSCREEN_GOODIX_BRL=y",
        "CONFIG_TOUCHSCREEN_GOODIX_BRL_MEIZU=y", "CONFIG_TOUCHSCREEN_NT36XXX_I2C=n",
        "CONFIG_TOUCHSCREEN_ATMEL_MXT=n", "CONFIG_TOUCHSCREEN_DUMMY=n",
    ])
    maybe("mmrm", "qcom/opensource/mmrm-driver", [f"MMRM_ROOT={ext}/qcom/opensource/mmrm-driver"])
    maybe("smcinvoke", f"{secure}/smcinvoke")
    maybe("qrng", f"{secure}/qrng")
    maybe("crypto-qti", f"{secure}/crypto-qti")
    maybe("tz_log", f"{secure}/tz_log")
    maybe("rpmb-control", "meizu/rpmb-control")
    maybe("hdcp", f"{secure}/hdcp")
    for p in ["meizu/boot-mode", "meizu/haptic-hv", "meizu/pressure-f71", "meizu/pressure-m68",
              "nxp/opensource/uwb", "st/opensource/st54se-gpio", "nxp/opensource/driver", "st/opensource/nfc"]:
        maybe(p, p)

    maybe("msm_ext_display", f"{mm}/msm_ext_display", [f"MSM_EXT_DISPLAY_ROOT={ext}/{mm}/"])
    maybe("sync_fence", f"{mm}/sync_fence", [f"SYNC_FENCE_ROOT={ext}/{mm}/"])
    maybe("hw_fence", f"{mm}/hw_fence", [f"MSM_HW_FENCE_ROOT={ext}/{mm}/"])
    maybe("display", f"{display}/msm", [f"DISPLAY_ROOT={ext}/{display}", f"KERNEL_SRC={ack}", "CONFIG_ARCH_KALAMA=y"])
    maybe("gpu", gpu, [
        f"KGSL_PATH={ext}/{gpu}", f"KERNEL_SRC={ack}", "CONFIG_ARCH_KALAMA=y",
        "CONFIG_QCOM_KGSL=m", "CONFIG_ARM_SMMU=y", "CONFIG_CORESIGHT=", "CONFIG_QCOM_KGSL_MEIZU_THERMAL_LOG=n",
    ])
    maybe("audio", audio, [
        f"AUDIO_ROOT={ext}/{audio}", kcflags_str + f" -I{ext}/{audio}/include",
        "MODNAME=audio_dlkm", "BOARD_PLATFORM=kalama", "CONFIG_ARCH_KALAMA=y",
    ])
    maybe("ipa", ipa, [f"DATAIPA_ROOT={ext}/{ipa}", "CONFIG_ARCH_KALAMA=y", "CONFIG_IPA3=m", "CONFIG_IPA3_MHI_PRIME_MANAGER=n"])
    maybe("camera", camera, [
        f"CAMERA_KERNEL_ROOT={ext}/{camera}", f"KERNEL_ROOT={ack}", f"KERNEL_SRC={ack}",
        "CONFIG_SPECTRA_CAMERA=m", "BOARD_PLATFORM=kalama", "MODNAME=camera",
    ])
    maybe("video", video, [f"VIDEO_ROOT={ext}/{video}", "CONFIG_ARCH_KALAMA=y", "BOARD_PLATFORM=kalama"])
    maybe("eva", eva, [f"EVA_ROOT={ext}/{eva}", "CONFIG_ARCH_KALAMA=y", "BOARD_PLATFORM=kalama"])
    maybe("wlan", wlan, [
        f"WLAN_PLATFORM_ROOT={ext}/{wlan}", "CONFIG_CNSS_OUT_OF_TREE=y",
        "CONFIG_CNSS_GENL=m", "CONFIG_WCNSS_MEM_PRE_ALLOC=m", "CONFIG_CNSS_UTILS=m",
        "CONFIG_CNSS_QMI_SVC=m", "CONFIG_CNSS_PLAT_IPC_QMI_SVC=m",
        "CONFIG_CNSS2=m", "CONFIG_CNSS2_QMI=y", "CONFIG_PCI_MSM=y", "CONFIG_CNSS_HW_SECURE_DISABLE=y",
    ])
    maybe("cld", cld, [
        "ANDROID_BUILD_TOP=", f"WLAN_PLATFORM_ROOT={ext}/{wlan}", f"WLAN_ROOT={ext}/{cld}",
        "WLAN_COMMON_ROOT=cmn", f"WLAN_COMMON_INC={ext}/{wifi}/qca-wifi-host-cmn",
        f"WLAN_FW_API={ext}/{wifi}/fw-api", "WLAN_PROFILE=kiwi_v2", "DYNAMIC_SINGLE_CHIP=kiwi_v2",
        "DEVNAME=kiwi_v2", "WLAN_CTRL_NAME=wlan", "CONFIG_QCA_CLD_WLAN=m", "CONFIG_BUS_AUTO_SUSPEND=y",
        "CONFIG_ARCH_KALAMA=y", "CONFIG_CNSS_OUT_OF_TREE=y", "CONFIG_CNSS2=m", "CONFIG_CNSS2_QMI=y", "CONFIG_PCI_MSM=y",
        "CONFIG_CNSS_UTILS=m", "CONFIG_CNSS_GENL=m", "CONFIG_WCNSS_MEM_PRE_ALLOC=m",
        "CONFIG_CNSS_QMI_SVC=m", "CONFIG_CNSS_PLAT_IPC_QMI_SVC=m", "CONFIG_CNSS_HW_SECURE_DISABLE=y",
        "BOARD_PLATFORM=kalama", "MODNAME=kiwi_v2",
    ])
    maybe("bt", bt, [
        f"BT_ROOT={ext}/{bt}", "CONFIG_MSM_BT_POWER=m", "CONFIG_BTFM_SLIM=m",
        "CONFIG_SLIM_BTFM_CODEC=n", "CONFIG_I2C_RTC6226_QCA=m",
    ])
    maybe("rmnet", rmnet, [f"RMNET_CORE_ROOT={ext}/{rmnet}", f"RMNET_CORE_INC_DIR={ext}/{rmnet}", "CONFIG_ARCH_KALAMA=y"])
    for feat in ["aps", "offload", "perf", "perf_tether", "sch", "shs", "wlan"]:
        maybe(f"rmnet-{feat}", f"qcom/opensource/datarmnet-ext/{feat}", ["CONFIG_ARCH_KALAMA=y", f"RMNET_CORE_INC_DIR={ext}/{rmnet}"])

    for kv in stage.rglob("kiwi_v2.ko"):
        kv.rename(kv.with_name("qca_cld3_kiwi_v2.ko"))

    print("OUTTREE BUILD COMPLETE!", flush=True)


def main():
    p = argparse.ArgumentParser(description="Meizu 20 (M2381) GKI 6.18 / ACK 6.18-lts module builder")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch-gki", help="Download official GKI Image, vmlinux.symvers, and signed system_dlkm modules")
    f.add_argument("--build-id", required=True, help="Google CI build ID, e.g. 17035987 (android17-6.18-2026-09_r17)")
    f.add_argument("--out", required=True, help="Destination directory")

    b = sub.add_parser("build-modules", help="Build decoupled out-of-tree modules against pure ACK 6.18 / ACK 6.18-lts")
    b.add_argument("--kernel-src", required=True, help="Path to ACK 6.18 / ACK 6.18-lts source tree")
    b.add_argument("--kernel-out", required=True, help="Path to prepared kernel O= build directory")
    b.add_argument("--modules-src", required=True, help="Path to android_kernel_meizu_m2381-modules")
    b.add_argument("--extra-symvers", required=True, help="Path to baseline Module.symvers (in-tree + vmlinux)")
    b.add_argument("--stage", required=True, help="INSTALL_MOD_PATH staging directory")
    b.add_argument("--clang-bin", default="", help="Optional path to AOSP Clang bin directory")
    b.add_argument("--jobs", type=int, default=os.cpu_count() or 8)
    b.add_argument("--only", default="", help="Comma-separated subset of modules to build (e.g. audio,display,gpu)")

    args = p.parse_args()
    if args.cmd == "fetch-gki":
        fetch_gki_artifacts(args.build_id, pathlib.Path(args.out))
    elif args.cmd == "build-modules":
        build_outtree(args)


if __name__ == "__main__":
    main()
