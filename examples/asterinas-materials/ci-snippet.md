# 从 CI 配置里抄出来的（.github/workflows 摘录，供参考）

jobs 里编译那步实际执行的就是：

    docker run --rm --privileged --network=host -v /dev:/dev \
      -v $REPO:/root/asterinas asterinas/dev:0.18.1-20260805 \
      bash -c 'cd /root/asterinas && make kernel'

启动测试那步：

    docker run --rm --privileged --network=host -v /dev:/dev \
      -v $REPO:/root/asterinas asterinas/dev:0.18.1-20260805 \
      bash -c 'cd /root/asterinas && make run_kernel AUTO_TEST=boot'

CI 上偶发挂：QEMU 随机 hostfwd 端口撞上宿主机已占端口，qemu.log 空且
make 报 Error——重跑就好，是瞬时环境故障不是代码问题。
