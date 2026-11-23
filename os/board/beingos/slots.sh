# Stable lab slot IDs shared by GPT, GRUB and RAUC.
slot_a_uuid=bbafccdc-0615-4e77-ab86-032633aedd4a
slot_b_uuid=14c028c4-8d14-415c-af2f-4348c7f5893b

slot_ids() {
    sed "s/SLOT_A_UUID/$slot_a_uuid/g; s/SLOT_B_UUID/$slot_b_uuid/g; s/INSTALL_ESP_UUID//g; s/INSTALL_MODELS_UUID//g; s/INSTALL_MIND_UUID//g" "$1"
}

# Kernel arguments the GRUB templates carry as tokens:
#   BEING_LAPTOP_ARGS  every laptop boot entry: the top row sends F1-F12
#                      without fn (hid_apple is built in, so it is a boot
#                      argument, not a modprobe option). /dev/cpu/*/msr is
#                      read-only until bodycare's battery-out mode (plan
#                      section 4, Mode B) turns writes on at run time.
#   BEING_QUIET_ARGS   the normal installed laptop boot: only the eyes show,
#                      no cursor and no systemd status unless something fails.
being_laptop_args='hid_apple.fnmode=2 msr.allow_writes=off'
being_quiet_args='quiet vt.global_cursor_default=0 systemd.show_status=error'

# The laptop image (BR2_ROOTFS_POST_SCRIPT_ARGS="laptop") has no serial line:
# drop GRUB's serial terminal and the kernel's serial console. Its boots never
# turn an oops into a panic: on a confirmed slot that is an endless reboot
# loop with nothing on the screen to read. The lab keeps oops=panic so the
# QEMU suites see a driver bug as a failed boot.
boot_console() {
    if [ "${BEINGOS_VARIANT:-lab}" = laptop ]; then
        sed -e '/^serial --unit=/d' -e '/^terminal_output serial$/d' -e 's/ console=ttyS0,115200//g' \
            -e 's/ oops=panic//g' \
            -e "s/ BEING_LAPTOP_ARGS/ $being_laptop_args/g" -e "s/ BEING_QUIET_ARGS/ $being_quiet_args/g"
    else
        sed -e 's/ BEING_LAPTOP_ARGS//g' -e 's/ BEING_QUIET_ARGS//g'
    fi
}
