# Run every container under one host name

- Date: 2026-10-22
- Supersedes: none

Chromium's `SingletonLock` names its writer as `<host>-<pid>`. A launch
clears a lock that names its own host and a pid that has gone or is not a
Chromium, and refuses one that names any other host: the profile is "in use
by another Chromium process on another computer".
`tests/test_docker_hostname.py` measures each case.

Docker gives every new container a new host name. A container killed while
Chromium runs leaves its lock on the mounted profile, and every later
container refused that profile until the three `Singleton*` files were
deleted by hand. A profile made by `--login --login-viewer` is opened
directly, so it is the one that sticks.

So `docker-compose.yml` sets `hostname: linkedin-mcp`, and every documented
`docker run` passes `--hostname linkedin-mcp`. Two containers on one mounted
profile then no longer refuse each other through Chromium's lock. The profile
lease, a file lock under the mounted auth root, should keep them apart as it
does two servers on one machine, but that is unmeasured: no two containers
sharing the host name have been run against one profile.

Containers sharing the host name also share `profile_in_use_by`'s same-host
branch, which probes the lock's pid, although each container numbers its own
processes. So a live pid counts only when its process name (`/proc/<pid>/comm`)
is a Chromium's, as Chromium checks the pid's executable; any other is a
left-over lock. Without `/proc`, as on macOS, a live pid still counts.
#1228's refusal of an unverifiable lock now applies only to a host name other
than this one, such as a container started without the fixed name.
