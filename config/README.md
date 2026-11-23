# 项目配置

根目录的编译、打包、测试和代码检查规则统一放在此目录。应用自身的配置仍由所属应用或包维护；`config/` 不存放模型凭据和患者数据。

| 配置 | 用途 |
|---|---|
| `tsconfig.base.json` | 公共 TypeScript 规则和源码路径映射 |
| `tsconfig.base.client.json` | 浏览器端公共规则及类型目录 |
| `tsconfig.node.base.json` | Node 环境公共规则 |
| `tsconfig.host.json` | 服务端编译引用及检查入口 |
| `tsconfig.client.json` | 浏览器端编译引用及检查入口 |
| `tsdown.config.ts` | 工作区打包入口 |
| `vitest.config.ts` | 常规测试配置 |
| `vitest.shared.ts` | 测试公共转换器和运行参数 |
| `vitest.e2e.config.ts` | 端到端测试配置 |
| `vitest.web.config.ts` | Web 交互测试配置 |
| `vitest.web.perf.config.ts` | Web 性能测试配置 |
| `vitest.web-stress.config.ts` | Web 压力测试配置 |
| `oxlint.json` | 完整代码检查规则 |
| `oxlint-staged.json` | 不要求完整类型信息的检查规则 |
| `jscpd.json` | 重复代码检查规则 |

## 运行方式

原有 `pnpm build`、`pnpm dev:web`、`pnpm lint`、`pnpm duplication`、`pnpm test:oral`、`pnpm test:framework`、`pnpm test:e2e` 和 `pnpm test:web` 命令已同步引用新位置。

运行指定测试使用：

```sh
pnpm vitest run scripts/repository-config.spec.ts
```

直接执行测试工具时须明确指定配置：

```sh
pnpm exec vitest run --config config/vitest.config.ts
```

直接打包也须指定配置：

```sh
pnpm exec tsdown --config config/tsdown.config.ts --config-loader native
```

路径别名生成器、编译器装配、覆盖率分片、开发监听、源码子进程和沙箱中的 tsx 注册均已同步。相对路径以配置实际所在目录为基准，工作区扫描和测试运行的根目录仍是项目根目录。

## 根目录保留项

- `package.json`：Node/pnpm 的包清单与命令入口。
- `pnpm-workspace.yaml`：pnpm 工作区发现入口。
- `pnpm-lock.yaml`：pnpm 默认使用的依赖锁文件。
- `tsconfig.json`：供编辑器、tsx 和 TypeScript 自动发现的简短 solution 入口，仅转向本目录的规则与两个编译面。
- `pytest.ini`、Git 和编辑器的约定文件继续保留在根目录，维持各自的自动发现行为。

迁移范围为原根目录的 15 个配置文件。包内的 `tsconfig` 及其 `extends` 路径已同步调整；没有修改医疗问询、记忆、医生复核或疾病规则。

## 迁移验证

冻结锁文件安装、7 阶段完整构建、241 项相关测试、39 项医疗业务测试、MCP/患者预设/医生表单检查、真实 Web 启动和路径别名检查通过。测试覆盖根 solution 的源码解析、两套编译面的根目录、外部工作区配置、开发监听、覆盖率分片及源码子进程启动路径。

此记录仅说明本次迁移的验证范围，不代表全部历史上游测试或真实模型问诊已完成验证。

常规测试直接从 `tsconfig.base.json` 建立源码别名，覆盖 `integrations/` 下不属于 pnpm 包的业务测试；不会依赖根 solution 的空 `files` 列表做发现。
