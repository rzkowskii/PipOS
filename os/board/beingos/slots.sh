# Stable lab slot IDs shared by GPT, GRUB and RAUC.
slot_a_uuid=bbafccdc-0615-4e77-ab86-032633aedd4a
slot_b_uuid=14c028c4-8d14-415c-af2f-4348c7f5893b

slot_ids() {
    sed "s/SLOT_A_UUID/$slot_a_uuid/g; s/SLOT_B_UUID/$slot_b_uuid/g; s/INSTALL_ESP_UUID//g; s/INSTALL_MODELS_UUID//g; s/INSTALL_MIND_UUID//g" "$1"
}

# The laptop image (BR2_ROOTFS_POST_SCRIPT_ARGS="laptop") has no serial line:
# drop GRUB's serial terminal and the kernel's serial console.
boot_console() {
    if [ "${BEINGOS_VARIANT:-lab}" = laptop ]; then
        sed -e '/^serial --unit=/d' -e '/^terminal_output serial$/d' -e 's/ console=ttyS0,115200//g'
    else
        cat
    fi
}
