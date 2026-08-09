import os
rule = (
    "devconph ALL=(ALL) NOPASSWD: "
    "/usr/sbin/modprobe usbserial, "
    "/usr/sbin/insmod /home/devconph/Documents/kai/hardware/ch341_build/ch341.ko, "
    "/usr/bin/bash /home/devconph/Documents/kai/hardware/fix_usb.sh\n"
)
path = "/etc/sudoers.d/face-servo"
open(path, "w").write(rule)
os.chmod(path, 0o440)
print("Done:", path)
