#!/system/bin/sh
case "$(uname -r)" in
    6.18.*)
        exec /system/bin/sh /data/adb/m2381_gki61832/loader.sh
        ;;
    *)
        exit 0
        ;;
esac
