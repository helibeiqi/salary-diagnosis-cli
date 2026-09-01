/**
 * index.ts — dsh Cordis 插件入口三件套（架构 §2.1）
 * ============================================================================
 *
 * `name` / `inject` / `apply` 三个命名导出是 cordis loader 的固定契约，
 * 缺一或改名都会导致插件不被加载。
 *
 * ⚠️ **绝不 `export const Config`**
 * ---------------------------------------------------------------------------
 * 非 schemastery 对象会让 cordis loader 启动时崩溃。第一方插件若要声明配置，
 * 必须用 `z.object({...})` 构造（见 `dsh-tool-todo/lib/index.js:19`）。
 * 本项目不需要任何用户可配项 —— pythonExe / projectRoot 全走
 * `apply(ctx, config)` 的可选参数 + 环境变量 —— 因此**干脆不导出 Config**，
 * 从根上消除这个踩坑面。
 */

// ★ ESM + NodeNext：相对导入**必须**写 .js 后缀（指向编译产物），
//   TS 会在编译期自动映射到 .ts 源码。写 .ts 需要 allowImportingTsExtensions，
//   而该选项与产出 lib/ 互斥（它要求 noEmit）。
import { CompToolService, type PluginContext, type CompToolConfig } from './service.js';

/** 插件名：同时是 `cordis.patch.yml` 里 `- insert:` 条目的 id。 */
export const name = 'comp-tool';

/** 声明依赖 `tools` 服务。`as const` 让 loader 能静态读到注入列表。 */
export const inject = ['tools'] as const;

/**
 * 插件入口。刻意保持极薄 —— 所有逻辑都在 `CompToolService` 里，
 * 这样测试和未来的 HMR 都不需要经过 cordis loader。
 *
 * ★ 为什么 ctx 类型是本地 `PluginContext` 而不是 cordis 的 `Context`
 *   （与 dsh-excel-kit 金标准同解）：
 *   `tools` 是**运行时**由 `inject = ['tools']` 注入的服务，cordis 的静态
 *   `Context` 类型上并没有这个属性，直接用会报
 *   `Property 'tools' is missing in type 'Context'`。
 *   为此再去做 module augmentation 得不偿失 —— 本项目只用到
 *   register / on / get / session 四个面，用最小结构类型描述即可，
 *   同时让插件在脱离 dsh 宿主时仍可单测（传个假 ctx 就行）。
 */
export function apply(ctx: PluginContext, config?: CompToolConfig): void {
  new CompToolService(ctx, config).register();
}

/** 便于宿主或测试直接取用类型与实现（dsh-excel-kit 同样做了再导出）。 */
export { CompToolService, type PluginContext, type CompToolConfig };
