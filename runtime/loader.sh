#!/system/bin/sh
set -u
payload=/data/adb/m2381_gki61832
mode=${1:-load}
kver=$(uname -r)

case "$mode" in
    load)
        case "$kver" in
            6.18.*) ;;
            *) exit 0 ;;
        esac
        ;;
    --check) ;;
    *) exit 2 ;;
esac

if [ "$mode" = load ]; then
    exec >>"$payload/load.log" 2>&1
    printf "\nStarting %s (%s)\n" "$(date)" "$kver"
fi
cd "$payload" || exit 1
if [ "$mode" = --check ]; then
    sha256sum -c SHA256SUMS > /dev/null || exit 1
    printf "Verified %s module files\n" "$(wc -l < modules.load)"
    exit 0
fi

# If per-kernel signed GKI system_dlkm overrides exist, overlay them into active payload
if [ -d "$payload/sysdlkm/$kver" ]; then
    for ko in "$payload/sysdlkm/$kver"/*.ko; do
        [ -f "$ko" ] && cp -f "$ko" "$payload/$(basename "$ko")"
    done
fi

[ -w /sys/module/pcie_aspm/parameters/policy ] && echo powersupersave > /sys/module/pcie_aspm/parameters/policy 2>/dev/null || true

status=0
if [ -x "$payload/fastmod" ]; then
    if ! "$payload/fastmod" -j 8 "$payload" "$payload/modules.load"; then
        status=1
    fi
else
    while IFS= read -r module; do
        name=${module%.ko}
        name=$(printf "%s" "$name" | tr "-" "_")
        [ ! -d "/sys/module/$name" ] || continue
        if ! insmod "$payload/$module" && [ ! -d "/sys/module/$name" ]; then
            printf "Failed %s\n" "$module"
            status=1
        fi
    done < modules.load
fi

if [ -w /proc/sys/walt/panic_on_walt_bug ]; then
    echo 1162141240 > /proc/sys/walt/panic_on_walt_bug
fi
if [ -d /sys/class/meizu/motor ]; then
    chown system:system /sys/class/meizu/motor/on_off /sys/class/meizu/motor/enable /sys/class/meizu/motor/set_cspress /sys/class/meizu/motor/set_mback /sys/class/meizu/motor/rtp /sys/class/meizu/motor/game /sys/class/meizu/motor/set_rtp /sys/class/meizu/motor/waveform /sys/class/meizu/motor/freq /sys/class/meizu/motor/usr_f0 2>/dev/null || true
    chmod 0666 /sys/class/meizu/motor/on_off 2>/dev/null || true
    chmod 0660 /sys/class/meizu/motor/enable /sys/class/meizu/motor/set_cspress /sys/class/meizu/motor/set_mback /sys/class/meizu/motor/rtp /sys/class/meizu/motor/game /sys/class/meizu/motor/set_rtp /sys/class/meizu/motor/waveform /sys/class/meizu/motor/freq /sys/class/meizu/motor/usr_f0 2>/dev/null || true
    chown system:audio /sys/class/meizu/motor/haptic_audio 2>/dev/null || true
    chmod 0660 /sys/class/meizu/motor/haptic_audio 2>/dev/null || true
fi
if [ -e /sys/class/timed_output/vibrator/enable ]; then
    chown system:system /sys/class/timed_output/vibrator/enable
    chmod 0666 /sys/class/timed_output/vibrator/enable
fi
if [ -e /dev/nq-nci ]; then
    setprop ctl.restart nqnfc_2_0_hal_service
fi
if [ -r /sys/class/tty/ttyMZH0/dev ] && [ ! -e /dev/ttyHS0 ]; then
    IFS=: read -r maj min < /sys/class/tty/ttyMZH0/dev
    mknod /dev/ttyHS0 c "$maj" "$min" 2>/dev/null || true
    chown bluetooth:net_bt /dev/ttyHS0 2>/dev/null || true
    chmod 0660 /dev/ttyHS0 2>/dev/null || true
    chcon u:object_r:hci_attach_dev:s0 /dev/ttyHS0 2>/dev/null || true
fi
printf "Finished status=%s\n" "$status"
exit "$status"
