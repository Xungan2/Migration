# docker 资源锁（docker-resource-lock）

**签名**：build/boot 命令长时间挂起或立即失败，输出含
`resource temporarily unavailable` / `database is locked` /
容器名冲突（"The container name ... is already in use"）/
`Cannot connect to the Docker daemon` / `no space left on device`。

**判别**：rc≠0 + 上述文案；同命令此前刚跑过（events 可查——
并发/残留容器是常见诱因）。

**归责**：infra

**建议动作**：`rerun` ——幂等重跑即愈（瞬时资源竞争）；
反复出现才需人工清理容器/磁盘。

**实证**：e2e-test-retry 早期镜像锁卡 25min（纯等待浪费）。
