# Copyright (c) 2021, Xilinx®
# All rights reserved
#
# Author: Víctor Mayoral Vilches <victorma@xilinx.com>

import os
import sys

from colcon_core.plugin_system import satisfies_version
from colcon_krs.subverb import (
    KRSSubverbExtensionPoint,
    get_vitis_dir,
    get_rawimage_path,
    get_firmware_dir,
    mount_rawimage,
    umount_rawimage,
    run,
    mountpoint1,
    mountpoint2,
    mountpointn,
    replace_kernel,
    add_kernel,
    exists,
)
from colcon_krs.verb import green, yellow, red, gray

## Only dom0
TEMPLATE_CONFIG = """\
MEMORY_START=0x0
MEMORY_END=0x80000000
DEVICE_TREE=system.dtb
BOOTBIN=BOOT.BIN
XEN=xen
UBOOT_SOURCE=boot.source
UBOOT_SCRIPT=boot.scr
"""


class HypervisorSubverb(KRSSubverbExtensionPoint):
    """
    Configure the Xen hypervisor.
    """

    def __init__(self):  # noqa: D107
        super().__init__()
        satisfies_version(KRSSubverbExtensionPoint.EXTENSION_POINT_VERSION, "^1.0")

    def add_arguments(self, *, parser):  # noqa: D102

        # debug arg, show configuration and leave temp. dir (do not delete)
        argument = parser.add_argument("--debug", action="store_true", default=False)

        # dom0 VM
        argument = parser.add_argument(
            "--dom0", action="store", dest="dom0_arg", choices=["preempt_rt", "vanilla"]
        )

        # domU VMs
        argument = parser.add_argument(
            "--domU",
            action="append",
            dest="domU_args",
            choices=["preempt_rt", "vanilla"],
            # nargs="+",
        )

        # dom0less VMs
        argument = parser.add_argument(
            "--dom0less",
            action="append",
            dest="dom0less_args",
            choices=["preempt_rt", "vanilla"],
        )

        # VMs ramdisks (dom0 is NOT included)
        argument = parser.add_argument(
            "--ramdisk",
            action="append",
            dest="ramdisk_args",
            help="ramdisks for VMs, excluding dom0.",
        )

        argument = parser.add_argument(
            "--rootfs",
            action="append",
            dest="rootfs_args",
            help="rootfs' for VMs, including dom0.",
        )

        try:
            from argcomplete.completers import ChoicesCompleter
        except ImportError:
            pass
        else:
            type_options = ["vanilla", "preempt_rt"]
            argument.completer = ChoicesCompleter(type_options)

        # remember the subparser to print usage in case no subverb is passed
        self.parser = parser

    def default_hypervisor_setup(self, context):
        """
        Default image setup using:
            - dom0 and
            - dom0less machine with a busybox ramdisk
        """
        firmware_dir = get_firmware_dir()

        # create auxiliary directory for compiling all artifacts for the hypervisor
        auxdir = "/tmp/hypervisor"
        run("mkdir " + auxdir, shell=True, timeout=1)

        # copy the artifacts to auxiliary directory
        run(
            "cp " + firmware_dir + "/bootbin/BOOT.BIN.xen " + auxdir + "/BOOT.BIN",
            shell=True,
            timeout=1,
        )
        run(
            "cp " + firmware_dir + "/kernel/Image " + auxdir + "/Image",
            shell=True,
            timeout=1,
        )
        run("cp " + firmware_dir + "/xen " + auxdir + "/xen", shell=True, timeout=1)
        run(
            "cp "
            + firmware_dir
            + "/device_tree/system.dtb.xen "
            + auxdir
            + "/system.dtb",
            shell=True,
            timeout=1,
        )
        run(
            "cp " + firmware_dir + "/initrd.cpio " + auxdir + "/initrd.cpio",
            shell=True,
            timeout=1,
        )

        # produce config
        config = open(auxdir + "/xen.cfg", "w")
        config.truncate(0)  # delete previous content
        config.write(TEMPLATE_CONFIG)
        config.close()

        # generate boot script
        imagebuilder_dir = firmware_dir + "/imagebuilder"
        imagebuilder_path = imagebuilder_dir + "/scripts/uboot-script-gen"
        cmd = (
            "cd "
            + auxdir
            + " && bash "
            + imagebuilder_path
            + ' -c xen.cfg -d . -t "load mmc 0:1"'
        )

        if context.args.debug:
            gray(cmd)

        outs, errs = run(cmd, shell=True, timeout=5)
        if errs:
            red("Something went wrong.\n" + "Review the output: " + errs)
            sys.exit(1)
        # print(outs)

        # mount sd_card image
        rawimage_path = get_rawimage_path("sd_card.img")
        mount_rawimage(rawimage_path, 1)

        # copy all artifacts
        cmd = "sudo cp " + auxdir + "/* " + mountpoint1 + "/"
        outs, errs = run(cmd, shell=True, timeout=5)
        if errs:
            red(
                "Something went wrong while replacing the boot script.\n"
                + "Review the output: "
                + errs
            )
            sys.exit(1)
        green("- Successfully copied all Xen artifacts.")

        # umount raw disk image, (technically, only p1)
        umount_rawimage(1)

        # cleanup auxdir
        if not context.args.debug:
            run("sudo rm -r " + auxdir, shell=True, timeout=1)

    def argument_checks(self, context):
        """
        Check arguments provided and ensure they're reasonable

        TODO: document arguments
        """
        # ensure ramdisks don't overrun domUs + dom0less
        # NOTE that dom0 doesn't count
        if (
            context.args.ramdisk_args
            and context.args.domU_args
            and context.args.dom0less_args
            and (
                len(context.args.domU_args) + len(context.args.dom0less_args)
                < len(context.args.ramdisk_args)
            )
            or context.args.ramdisk_args
            and context.args.dom0less_args
            and (len(context.args.dom0less_args) < len(context.args.ramdisk_args))
        ):
            red(
                "- More ramdisks provided than VMs. Note that dom0's ramdisk should NOT be indicated (ramdisks <= domUs + dom0less)."
            )
            sys.exit(1)

        # ensure rootfs don't overrun domUs + dom0less + dom0 (+1)
        if context.args.rootfs_args and (
            len(context.args.domU_args) + len(context.args.dom0less_args) + 1
            < len(context.args.rootfs_args)
        ):
            red(
                "- More rootfs provided than VMs, including dom0's (rootfs <= domUs + dom0less + 1)."
            )
            sys.exit(1)

        # ensure rootfs and ramdisks don't overrun domUs + dom0less + dom0 (+1)
        if (
            context.args.ramdisk_args
            and context.args.rootfs_args
            and (
                len(context.args.domU_args) + len(context.args.dom0less_args) + 1
                < len(context.args.ramdisk_args) + len(context.args.rootfs_args)
            )
        ):
            red(
                "- More rootfs and ramdisks provided than VMs, including dom0's (rootfs + ramdisks <= domUs + dom0less + 1)."
            )
            sys.exit(1)

        # inform if the domUs + dom0less + dom0 (+1) count is greater than rootfs + ramdisks count
        if (
            context.args.ramdisk_args
            and context.args.rootfs_args
            and (
                len(context.args.domU_args) + len(context.args.dom0less_args) + 1
                > len(context.args.ramdisk_args) + len(context.args.rootfs_args)
            )
        ):
            yellow("- More VMs than ramdisks and rootfs provided, will use defaults.")

        # # inform if ramdisks is lower than VMs
        # if not context.args.ramdisk_args:
        #     yellow(
        #         "- No ramdisks provided. Defaulting to " + str(default_ramdisk)
        #     )
        #
        # if context.args.ramdisk_args and (
        #     len(context.args.domU_args) > len(context.args.ramdisk_args)
        # ):
        #     yellow(
        #         "- Number of ramdisks is lower than domU VMs. "
        #         "Last "
        #         + str(
        #             len(context.args.domU_args) - len(context.args.ramdisk_args)
        #         )
        #         + " VM will default to: "
        #         + str(default_ramdisk)
        #    )

    def xen_fixes(self, partition=2):
        """
        Fixup Xen FS
        """
        firmware_dir = get_firmware_dir()

        # mount sd_card image
        rawimage_path = get_rawimage_path("sd_card.img")
        mount_rawimage(rawimage_path, partition)

        mountpoint_partition = mountpointn + str(partition)
        # create Xen missing dir
        cmd = "sudo mkdir -p " + mountpoint_partition + "/var/lib/xen"
        outs, errs = run(cmd, shell=True, timeout=5)
        if errs:
            red(
                "Something went wrong while creating Xen /var/lib/xen directory in rootfs.\n"
                + "Review the output: "
                + errs
            )
            sys.exit(1)
        green("- Successfully created Xen /var/lib/xen directory in rootfs.")

        # setup /etc/inittab for Xen
        cmd = (
            "sudo sed -i 's-PS0:12345:respawn:/bin/start_getty 115200 ttyPS0 vt102-X0:12345:respawn:/sbin/getty 115200 hvc0-g' "
            + mountpoint_partition
            + "/etc/inittab"
        )
        outs, errs = run(cmd, shell=True, timeout=5)
        if errs:
            red(
                "Something went wrong while setting up /etc/inittab for Xen in rootfs.\n"
                + "Review the output: "
                + errs
            )
            sys.exit(1)
        green("- Successfully setup /etc/inittab for Xen in rootfs.")

        # umount raw disk image
        umount_rawimage(partition)

    def main(self, *, context):  # noqa: D102
        """
        Create a Xen configuration, produce boot scripts and deploy
        corresponding files into partitions.

        TODO: ramdisk selection is currently not implemented.

        NOTE: Location, syntax and other related matters are defined
            within the `acceleration_firmware_xilinx` package. Refer to it for more
            details.

        NOTE 2: to simplify implementation, for now, domUs will use rootfs
        and dom0less ramdisks
        """

        # TODO: review in the future
        #
        # if context.args.domU_args and context.args.dom0less_args:
        #     red("Simultaneous use of domU and dom0less VMs not supported.")
        #     sys.exit(1)

        if not (
            context.args.dom0_arg
            or context.args.domU_args
            or context.args.dom0less_args
        ):
            # self.default_hypervisor_setup(context)
            red("Please provide dom0 args at least")
            sys.exit(0)

        num_domus = 0  # NUM_DOMUS element in the configuration, also used for iterate over DomUs
        num_dom0less = 0  # used to iterate over Dom0less
        global TEMPLATE_CONFIG
        default_ramdisk = "initrd.cpio"
        default_rootfs = (
            "rootfs.cpio.gz"  # note rootfs could be provided in cpio.gz or tar.gz
        )
        # see imagebuilder for more details

        # create auxiliary directory for compiling all artifacts for the hypervisor
        auxdir = "/tmp/hypervisor"
        run("mkdir " + auxdir, shell=True, timeout=1)

        firmware_dir = get_firmware_dir()  # directory where firmware is

        # save last image, delete rest
        if exists(firmware_dir + "/sd_card.img"):
            if exists(firmware_dir + "/sd_card.img.old"):
                run(
                    "sudo rm " + firmware_dir + "/sd_card.img.old",
                    shell=True,
                    timeout=1,
                )
                yellow("- Detected previous sd_card.img.old raw image, deleting.")

            run(
                "sudo mv "
                + firmware_dir
                + "/sd_card.img "
                + firmware_dir
                + "/sd_card.img.old",
                shell=True,
                timeout=1,
            )
            yellow(
                "- Detected previous sd_card.img raw image, moving to sd_card.img.old."
            )

        #####################
        # process Dom0
        #####################
        if context.args.dom0_arg:

            # domU, dom0less, ramdisk and rootfs checks
            self.argument_checks(context)

            # replace Image in boot partition and assign silly ramdisk (not used)
            if context.args.dom0_arg == "vanilla":
                # copy to auxdir
                run(
                    "cp " + firmware_dir + "/kernel/Image " + auxdir + "/Image",
                    shell=True,
                    timeout=1,
                )
                TEMPLATE_CONFIG += "DOM0_KERNEL=Image\n"

            elif context.args.dom0_arg == "preempt_rt":
                # # directly to boot partition
                # replace_kernel("Image_PREEMPT_RT")

                # copy to auxdir
                run(
                    "cp "
                    + firmware_dir
                    + "/kernel/Image_PREEMPT_RT "
                    + auxdir
                    + "/Image_PREEMPT_RT",
                    shell=True,
                    timeout=1,
                )
                TEMPLATE_CONFIG += "DOM0_KERNEL=Image_PREEMPT_RT\n"
            else:
                red("Unrecognized dom0 arg.")
                sys.exit(1)

            # TEMPLATE_CONFIG += "DOM0_RAMDISK=initrd.cpio\n"  # ignored when using SD
            # green("- Dom0 rootfs assumed to reside in the second SD partition.")

            # Copy Dom0's rootfs
            if not context.args.rootfs_args or (len(context.args.rootfs_args) < 1):
                yellow(
                    "- No rootfs for Dom0 provided. Defaulting to "
                    + str(default_rootfs)
                )
                rootfs = default_rootfs
                assert exists(firmware_dir + "/" + rootfs)
                run(
                    "cp " + firmware_dir + "/" + rootfs + " " + auxdir + "/" + rootfs,
                    shell=True,
                    timeout=1,
                )
                green("- Copied to temporary directory rootfs: " + rootfs)
            else:
                rootfs = context.args.rootfs_args[num_domus]
                num_domus += 1  # jump over first rootfs arg
                # this way, list will be consistent
                # when interating over DomUs

            TEMPLATE_CONFIG += "DOM0_ROOTFS=" + str(rootfs) + "\n"

            #####################
            # process DomUs
            #####################
            if context.args.domU_args:
                for domu in context.args.domU_args:
                    # TODO: consider adding ramdisk support for domUs
                    # define rootfs for this domU, or default
                    if not context.args.rootfs_args or (
                        num_domus >= len(context.args.rootfs_args)
                    ):
                        rootfs = default_rootfs
                    else:
                        rootfs = context.args.rootfs_args[num_domus]

                    if domu == "vanilla":
                        # add_kernel("Image")  # directly to boot partition

                        # copy to auxdir
                        run(
                            "cp " + firmware_dir + "/kernel/Image " + auxdir + "/Image",
                            shell=True,
                            timeout=1,
                        )
                        TEMPLATE_CONFIG += (
                            "DOMU_KERNEL[" + str(num_domus) + ']="Image"\n'
                        )
                    elif domu == "preempt_rt":
                        # add_kernel("Image_PREEMPT_RT")  # directly to boot partition

                        # copy to auxdir
                        run(
                            "cp "
                            + firmware_dir
                            + "/kernel/Image_PREEMPT_RT "
                            + auxdir
                            + "/Image_PREEMPT_RT",
                            shell=True,
                            timeout=1,
                        )

                        TEMPLATE_CONFIG += (
                            "DOMU_KERNEL[" + str(num_domus) + ']="Image_PREEMPT_RT"\n'
                        )
                    else:
                        red("Unrecognized domU arg.")
                        sys.exit(1)

                    # Add rootfs
                    TEMPLATE_CONFIG += (
                        "DOMU_ROOTFS[" + str(num_domus) + ']="' + str(rootfs) + '"\n'
                    )
                    TEMPLATE_CONFIG += "DOMU_NOBOOT[" + str(num_domus) + "]=y\n"
                    num_domus += 1

            #####################
            # process Dom0less
            #####################
            if context.args.dom0less_args:
                for dom0less in context.args.dom0less_args:
                    # define ramdisk for this dom0less, or default
                    if not context.args.ramdisk_args or (
                        num_dom0less >= len(context.args.ramdisk_args)
                    ):
                        ramdisk = default_ramdisk
                    else:
                        ramdisk = context.args.ramdisk_args[num_dom0less]

                    if dom0less == "vanilla":
                        run(
                            "cp " + firmware_dir + "/kernel/Image " + auxdir + "/Image",
                            shell=True,
                            timeout=1,
                        )
                        TEMPLATE_CONFIG += (
                            "DOMU_KERNEL["
                            + str(num_dom0less + num_domus)
                            + ']="Image"\n'
                        )
                    elif dom0less == "preempt_rt":
                        # add_kernel("Image_PREEMPT_RT")
                        run(
                            "cp "
                            + firmware_dir
                            + "/kernel/Image_PREEMPT_RT "
                            + auxdir
                            + "/Image_PREEMPT_RT",
                            shell=True,
                            timeout=1,
                        )
                        TEMPLATE_CONFIG += (
                            "DOMU_KERNEL["
                            + str(num_dom0less + num_domus)
                            + ']="Image_PREEMPT_RT"\n'
                        )
                    else:
                        red("Unrecognized dom0less arg.")
                        sys.exit(1)

                    TEMPLATE_CONFIG += (
                        "DOMU_RAMDISK["
                        + str(num_dom0less + num_domus)
                        + ']="'
                        + str(ramdisk)
                        + '"\n'
                    )
                    num_dom0less += 1

            # account for Dom0less in the total as well
            num_domus += num_dom0less

            #####################
            # configuration and images
            #####################
            # Add NUM_DOMUS at the end
            TEMPLATE_CONFIG += "NUM_DOMUS=" + str(num_domus) + "\n"

            if context.args.debug:
                gray("Debugging config file:")
                gray(TEMPLATE_CONFIG)

            # copy the artifacts to auxiliary directory
            # TODO: figure out a way for "disk_image" to fetch also the BOOT.BIN
            run(
                "cp " + firmware_dir + "/bootbin/BOOT.BIN.xen " + auxdir + "/BOOT.BIN",
                shell=True,
                timeout=1,
            )
            run("cp " + firmware_dir + "/xen " + auxdir + "/xen", shell=True, timeout=1)
            run(
                "cp "
                + firmware_dir
                + "/device_tree/system.dtb.xen "
                + auxdir
                + "/system.dtb",
                shell=True,
                timeout=1,
            )

            # copy (at least) default ramdisk initrd.cpio and default rootfs rootfs.cpio.gz
            run(
                "cp "
                + firmware_dir
                + "/"
                + default_ramdisk
                + " "
                + auxdir
                + "/"
                + default_ramdisk,
                shell=True,
                timeout=1,
            )
            run(
                "cp "
                + firmware_dir
                + "/"
                + default_rootfs
                + " "
                + auxdir
                + "/"
                + default_rootfs,
                shell=True,
                timeout=1,
            )

            # add other ramdisks, if neccessary:
            if context.args.ramdisk_args:
                for ramdisk in context.args.ramdisk_args:
                    assert exists(firmware_dir + "/" + ramdisk)
                    run(
                        "cp "
                        + firmware_dir
                        + "/"
                        + ramdisk
                        + " "
                        + auxdir
                        + "/"
                        + ramdisk,
                        shell=True,
                        timeout=1,
                    )
                    green("- Copied to temporary directory ramdisk: " + ramdisk)

            # add other rootfs, if neccessary:
            if context.args.rootfs_args:
                for rootfs in context.args.rootfs_args:
                    assert exists(firmware_dir + "/" + rootfs)
                    run(
                        "cp "
                        + firmware_dir
                        + "/"
                        + rootfs
                        + " "
                        + auxdir
                        + "/"
                        + rootfs,
                        shell=True,
                        timeout=1,
                    )
                    green("- Copied to temporary directory rootfs: " + rootfs)

            # produce config
            config = open(auxdir + "/xen.cfg", "w")
            config.truncate(0)  # delete previous content
            config.write(TEMPLATE_CONFIG)
            config.close()

            # generate boot script
            yellow("- Generating boot script")
            imagebuilder_dir = firmware_dir + "/imagebuilder"
            imagebuilder_path_configscript = (
                imagebuilder_dir + "/scripts/uboot-script-gen"
            )
            cmd = (
                "cd "
                + auxdir
                + " && bash "
                + imagebuilder_path_configscript
                + " -c xen.cfg -d . -t sd"
            )

            if context.args.debug:
                gray(cmd)

            outs, errs = run(cmd, shell=True, timeout=5)
            if errs:
                red(
                    "Something went wrong while generating config file.\n"
                    + "Review the output: "
                    + errs
                )
                sys.exit(1)
            green("- Boot script ready")

            # create sd card image
            yellow(
                "- Creating new sd_card.img, previous one will be moved to sd_card.img.old. This will take a few seconds, hold on..."
            )
            whoami, errs = run("whoami", shell=True, timeout=1)
            if errs:
                red(
                    "Something went wrong while fetching username.\n"
                    + "Review the output: "
                    + errs
                )
                sys.exit(1)

            # build image, add 500 MB of slack on each rootfs-based partition
            imagebuilder_path_diskimage = imagebuilder_dir + "/scripts/disk_image"
            cmd = (
                "cd "
                + auxdir
                + " && sudo bash "
                + imagebuilder_path_diskimage
                + " -c xen.cfg -d . -t sd -w "
                + auxdir
                + " -o "
                + firmware_dir
                + "/sd_card.img "
                + "-s 500"
            )
            if context.args.debug:
                gray(cmd)
            outs, errs = run(cmd, shell=True)
            if errs:
                red(
                    "Something went wrong while creating sd card image.\n"
                    + "Review the output: "
                    + errs
                )
                sys.exit(1)
            green("- Image successfully created")

            # permissions of the newly created image
            cmd = (
                "sudo chown "
                + whoami
                + ":"
                + whoami
                + " "
                + firmware_dir
                + "/sd_card.img"
            )
            outs, errs = run(cmd, shell=True)
            if errs:
                red(
                    "Something went wrong while creating sd card image.\n"
                    + "Review the output: "
                    + errs
                )
                sys.exit(1)

            # ## use existing SD card image
            # # mount sd_card image
            # rawimage_path = get_rawimage_path("sd_card.img")
            # mount_rawimage(rawimage_path, 1)
            #
            # # copy all artifacts
            # cmd = "sudo cp " + auxdir + "/* " + mountpoint1 + "/"
            # outs, errs = run(cmd, shell=True, timeout=5)
            # if errs:
            #     red(
            #         "Something went wrong while replacing the boot script.\n"
            #         + "Review the output: "
            #         + errs
            #     )
            #     sys.exit(1)
            # green("- Successfully copied all Xen artifacts.")
            #
            # # umount raw disk image, (technically, only p1)
            # umount_rawimage(1)

            # Xen SD card fixes
            # creates missing tmp dirs for Xen proper functioning, configures /etc/inittab, etc.
            # TODO: review this overtime in case PetaLinux output becomes differently
            self.xen_fixes(partition=2)

            # apply fixes also to every domU
            if context.args.domU_args:
                for i in range(len(context.args.domU_args)):
                    self.xen_fixes(partition=i + 2 + 1)

            # cleanup auxdir
            if not context.args.debug:
                run("sudo rm -r " + auxdir, shell=True, timeout=1)

        else:
            red("No dom0 specified, doing nothing.")
