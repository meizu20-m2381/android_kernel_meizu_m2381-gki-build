# android_kernel_meizu_m2381-gki-build

Build orchestration, GKI bridge drivers, and fast parallel module loader for **Meizu 20 (M2381 / SM8550)** targeting **official prebuilt Android 17 GKI (`android17-6.18`)** and **self-built ACK 6.18-lts**.

## Repository Layout

- `build.py`: Parameterized builder supporting `fetch-gki` (official Google CI artifacts + 16 signed `system_dlkm` modules) and `build-modules` (builds [`android_kernel_meizu_m2381-modules`](https://github.com/meizu20-m2381/android_kernel_meizu_m2381-modules) against pure ACK 6.18 / ACK 6.18-lts with zero private kernel tree dependency).
- `standalone/`: Out-of-tree bridge drivers required only when targeting official prebuilt GKI (`walt`, `dwc3`, `ufs`, `geni`, `drm_dp`, `soc_usb`, `ice`, `tmecom`, `panel_regulator`, `eusb2`, `host`). Not needed when building from ACK 6.18-lts in-tree sources.
- `fastmod/`: Native ARM64 8-thread DAG `finit_module()` loader (~710 ms for 219 modules vs ~3.8 s serial `insmod`).
- `runtime/`: Boot-time module loader scripts (`00-m2381-618-loader.sh`, `loader.sh` supporting any `6.18.*` kernel and optional `sysdlkm/<uname -r>/` overrides).
- `configs/modules.load`: Ordered list of 219 runtime modules.

## Quick Start

1. Fetch official GKI release artifacts (e.g. `android17-6.18-2026-09_r17`, build `17035987`):
   ```sh
   python3 build.py fetch-gki --build-id 17035987 --out ./prebuilt-r17
   ```
2. Build decoupled out-of-tree modules against pure ACK 6.18 (or ACK 6.18-lts `O=out`):
   ```sh
   python3 build.py build-modules \
     --kernel-src /path/to/ack-6.18 \
     --kernel-out /path/to/out-gki \
     --modules-src /path/to/android_kernel_meizu_m2381-modules \
     --extra-symvers /path/to/intree-Module.symvers \
     --stage /path/to/stage \
     --clang-bin /path/to/clang/bin
   ```

## Note on GKI Protected Signed Modules

Official prebuilt GKI kernels enable `CONFIG_MODULE_SIG_PROTECT=y`. The 16 GKI `system_dlkm` modules (`rfkill`, `bluetooth`, `pwrseq-core`, `tipc`, etc.) must come from the matching build's `system_dlkm_staging_archive.tar.gz` (automatically extracted by `build.py fetch-gki` into `signed-sysdlkm/`). Self-built ACK 6.18-lts kernels without `CONFIG_MODULE_SIG_PROTECT_LIST` do not have this restriction.

## Upstream Sources

- Qualcomm/Meizu 6.18 bridge drivers (`standalone/{dwc3,ufs,geni,drm_dp,soc_usb,ice,tmecom,panel_regulator,eusb2,host}`): https://github.com/baizhi958216/android_kernel_meizu_m2391
- WALT scheduler (`standalone/walt`): https://git.codelinaro.org/clo/la/kernel/qcom (`kernel.lnx.6.12.r70-rel`)
- Out-of-tree modules: https://github.com/meizu20-m2381/android_kernel_meizu_m2381-modules
- ACK 6.18-lts kernel: https://github.com/meizu20-m2381/android_kernel_meizu_m2381-ack-6.18-lts
