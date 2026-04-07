// 运行时 self-check：只在 dev mode 下被 app.js 调用一次。把那些"看起来像
// dead code 但其实是有意保留的 kludge"行为锁住，被误删/误改时第一时间发现。
//
// 这个文件不是单元测试框架，刻意不引入 jest/vitest 之类的依赖：前端目前没有
// 测试构建链路。它的角色更像"production-safe 断言"：
//   1. 通过 isDebugMode() 三条件门禁，生产环境完全跳过；
//   2. 失败时打 console.error 并把第一条失败信息 surface 到一个 banner，让
//      开发者立刻注意到，而不是要去翻 devtools；
//   3. 不抛异常、不阻塞 app 启动 —— 即使某条 case 因为环境差异炸了，也不影
//      响主流程。
//
// 当前覆盖的契约：
//   - parseDate: 接受 "...Z" 结尾的 UTC 时间戳并按 UTC 解释（#21 kludge）
//   - formatNumber: |value| >= 1000 时把小数位钳到 2 位，无视 digits 参数（#22 kludge）
//
// 未来再加新的 kludge 时，把对应断言写到这里就行。

import { formatNumber, parseDate } from "./formatters.js";

function approxEqual(a, b, epsilon = 1e-6) {
  return Math.abs(a - b) <= epsilon;
}

function checkParseDateZSuffix() {
  // #21 锁定：parseDate 必须把 "Z" 结尾解析成 UTC，而不是当作"无时区"再
  // 按本地时间解释。验证方式：构造一个明确知道 UTC 毫秒的时间戳，比对结果。
  // 2026-04-06T00:00:00.000Z 对应 Unix epoch 1775433600000 ms。
  const date = parseDate("2026-04-06T00:00:00.000Z");
  if (!date) {
    return "#21 kludge 已失效：parseDate 无法解析 Z 后缀的 UTC 时间戳。";
  }
  const expected = Date.UTC(2026, 3, 6, 0, 0, 0);
  if (date.getTime() !== expected) {
    return `#21 kludge 已失效：parseDate("...Z") 返回 ${date.toISOString()}，期望 UTC ${new Date(expected).toISOString()}。`;
  }
  return null;
}

function checkFormatNumberDigitsClamp() {
  // #22 锁定：|value| >= 1000 时，formatNumber 把小数位强行钳到 2 位，
  // 即使调用方明确传 digits=6 也只会拿到 2 位精度。如果未来有人忘了这一点
  // 把钳位逻辑删掉、或者改阈值，这条 self-check 会立刻报警。
  //
  // 12345.6789012 在 digits=6 时应该返回 "12345.68"（钳到 2 位 + trim）。
  const clamped = formatNumber(12345.6789012, 6);
  if (clamped !== "12345.68") {
    return `#22 钳位已失效：formatNumber(12345.6789012, 6) 返回 "${clamped}"，期望 "12345.68"。`;
  }
  // |value| < 1000 时仍按 digits 显示 —— 验证 0.0001234 走 4 位精度路径。
  const small = formatNumber(0.0001234, 4);
  if (small !== "0.0001") {
    return `#22 小额精度路径已失效：formatNumber(0.0001234, 4) 返回 "${small}"，期望 "0.0001"。`;
  }
  return null;
}

function checkFormatNumberSmallValueDigits() {
  // 反向锁定：|value| < 1000 的场景下 digits=6 必须真的能拿到 6 位精度，
  // 避免有人"为了一致"把钳位扩展到所有数字，这会摧毁价格 / 资金费率展示。
  const result = formatNumber(0.123456789, 6);
  // trimTrailingZeros 会去掉尾零，0.123456789 → toFixed(6) → "0.123457"
  if (result !== "0.123457") {
    return `#22 高精度小额路径已漂移：formatNumber(0.123456789, 6) 返回 "${result}"，期望 "0.123457"。`;
  }
  return null;
}

const CHECKS = [
  ["parseDate Z 后缀 UTC 解析（#21）", checkParseDateZSuffix],
  ["formatNumber |value| >= 1000 钳位（#22）", checkFormatNumberDigitsClamp],
  ["formatNumber |value| < 1000 高精度路径（#22）", checkFormatNumberSmallValueDigits],
];

// 调用方：app.js installDebugHandle 之后调一次。返回 { passed, failed, firstFailureMessage }，
// 让 app.js 自己决定要不要触发 banner（避免本模块直接耦合 state / setFlash / renderBanners）。
export function runDevSelfChecks() {
  const failures = [];
  for (const [name, fn] of CHECKS) {
    try {
      const message = fn();
      if (message) {
        failures.push({ name, message });
      }
    } catch (error) {
      failures.push({
        name,
        message: `[self-check 抛异常] ${error instanceof Error ? error.message : String(error)}`,
      });
    }
  }
  if (failures.length > 0) {
    // eslint-disable-next-line no-console
    console.error("[dev-self-check] 部分 kludge 行为契约失效", failures);
  } else {
    // eslint-disable-next-line no-console
    console.info(`[dev-self-check] 已通过 ${CHECKS.length} 条契约。`);
  }
  return {
    passed: CHECKS.length - failures.length,
    failed: failures.length,
    firstFailureMessage: failures[0]?.message || null,
  };
}

// 暴露 approxEqual 是为了未来扩展时方便复用，避免再写一遍 epsilon 比对。
export { approxEqual };
