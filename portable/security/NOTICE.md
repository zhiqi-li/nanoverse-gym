Profiles derived from the Apache-2.0 licensed Moby profiles:

- [seccomp/default.json](https://github.com/moby/profiles/blob/main/seccomp/default.json), upstream blob 6e5e47e242892aaa290f916ba1045f9213ef0b84
- [apparmor/template.go](https://github.com/moby/profiles/blob/main/apparmor/template.go), upstream blob 99873f3730b1b4b9db197746f3f25dbe576d2eea

TTC adds clone, clone3, unshare, setns, mount, umount2 and pivot_root to the seccomp allowlist, including nested namespace creation after entering its user namespace. The default clone3 ENOSYS rule is removed. Other default denials remain.

The outer AppArmor profile retains default proc, sysfs and mount denials and permits executable files under the trusted /usr, /bin, /sbin, /lib, /lib32, /lib64 and /opt trees plus the TTC CLI. The SDK copies the verified TTC daemon to an immutable root-owned executable under /tmp/ttc-*/bin/ttcd. That executable transitions to a runtime child profile allowing private proc staging and namespace-local mounts; the child permits Unix and netlink sockets. Kernel user/mount namespaces, gVisor and ordinary file ownership provide its isolation. These policies grant no additional Linux capabilities. They require a kernel and host policy that permit unprivileged user namespaces.

Install the AppArmor profile on an AppArmor host with `sudo apparmor_parser -r portable/security/ttc.apparmor` before selecting strict mode. Strict mode must never fall back to unconfined policies or the unsafe TTC daemon flag.

