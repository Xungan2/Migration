<!-- hint-v1-spinor -->
# 构建提示（spi-nor 场景，OS 级结论直接用）

- 命令骨架（树路径用 `${PORTER_TARGET_OS_ROOT}`，别写死）：

```
docker run --rm --privileged --network=host -v /dev:/dev -v ${PORTER_TARGET_OS_ROOT}:/root/asterinas asterinas/dev:0.18.1-20260805 bash -c 'cd /root/asterinas && make kernel LOG_LEVEL=info'
```

- 超时档：全量 3000s（dm-zero 轮实测 166s，冷缓存保守上界）、增量
  600s（已实测增量 ~200s 量级）。树没构建过就按全量给。
- 成功特征用 ``Finished `dev` profile``：cargo 收尾行，全新构建与纯增量
  两条路径都必现。
- 避坑：
  - xorriso 的 "Writing to 'stdio:...iso' completed successfully." 不是
    稳健成功特征——纯增量复用缓存 bundle 时这行消失。
  - grub-rescue 的 locale warning（cannot open directory .../share/locale）
    良性，成功构建照样有。
  - 裸机跑不了：VDSO_LIBRARY_DIR 检查直接报错，必须进容器。
  - LOG_LEVEL=info 要带（默认 error 几乎无日志，且会被烤进内核命令行）；
    后续 boot/单测命令保持同值，避免 bundle 无谓重建。
