"""真实轨迹 × 机制：同一组实测基座曲线/步数，四种上下文策略的账目对照。

数据来源：用户提供的一条真实生产会话（29 轮 / 606 步 / 基座 0→726.3K /
命中 256.9M / 未命中 3.5M / 输出 444.7K）。

机制：
  baseline      全量回放（实测发生的——这条曲线就是它的真实上下文轨迹）
  managed-cur   现行 managed：轮闭合即整理，精修索引渲染（≈原文 5%）
  managed-v3    V3 水位批量整理（渲染比例同现行，只隔离"整理频率"变量）
  managed-v3s   V3 + 批量合并狠压（合并索引压到原文 2%，ZCode 44× 锚点量级）

命中模型 v2（2026-09-06 修正；v1 把重组和轮内增长按步摊派，偏保守）：
  轮内追加式两种模式同构 → 轮内首发 miss 相同，直接取实测 3.5M 按轮
  内容量分摊（含思考块、序列化摩擦等全部真实成分，对 managed
  是保守取值——managed 实测步数更少、重读更少，只会更低）；
  managed 的额外 miss 只有每轮一次的装配重组：近 2 轮全量换位 + 尾部
  重写（TAIL），一次性计价，不按步摊派。

校准：baseline 的 Σ 步数×基座 按实测计费（260.4M）归一，同一系数
应用到 managed 曲线。整理成本按实测观察值校准（约 4-6K/次）。
"""

HIT, MISS, OUT = 0.115, 0.4, 1.4   # GLM-5.3-flash 半价 元/M
RENDER_CUR = 0.05                   # 精修索引渲染比（确定性回放实测校准）
RENDER_MERGED = 0.02                # 批量合并狠压：跨轮去重后的工作卡片
TAIL = 15_000                       # 状态 + 任务索引 + 文件地图（逐轮重写）
ORG_FIXED = 4_000                   # 整理调用固定开销（提示词 + JSON 输出）
ORG_PER_ROUND = 1_000               # 批内每轮的索引展开
WATERMARK = 100_000                 # V3 水位（未整理内容触发批量整理）
REAL_MISS = 260.4e6 - 256.9e6      # 实测未命中总量（轮内首发 + 全部真实摩擦）

BASES_K = [0, 98.9, 182.9, 236.7, 268, 269.1, 299.9, 304.5, 353.9, 369.6,
           371.1, 388.1, 414.4, 430.3, 464.2, 481.3, 510.6, 527.7, 553,
           558.4, 586.3, 589.6, 617.3, 640, 657.4, 680.5, 705.7, 711.9, 726.3]
STEPS = [29, 63, 44, 22, 1, 26, 2, 40, 16, 1, 5, 21, 15, 24, 16, 27, 22,
         27, 9, 34, 7, 34, 25, 15, 23, 27, 6, 17, 8]


def main() -> None:
    n = len(BASES_K)
    bases = [b * 1000 for b in BASES_K]
    deltas = [bases[i + 1] - bases[i] for i in range(n - 1)] + [14_400]
    total_delta = sum(deltas)

    # 实测校准：baseline 的模型计费 ÷ 实际计费
    model_bill = sum(s * b for s, b in zip(STEPS, bases))
    actual_bill = 260.4e6
    calib = actual_bill / model_bill

    def simulate(mode: str):
        rows = []
        cum_input = cum_yuan = 0.0
        tot_hit = tot_miss = 0.0
        fresh = 0.0
        org_events = 0
        for k in range(n):
            s, delta = STEPS[k], deltas[k]
            if mode == "baseline":
                ctx = bases[k] + delta / 2                     # 轮中均值
                billing = s * ctx * calib
                hit_tok = billing * (256.9e6 / 260.4e6)        # 实测命中占比
                miss_tok = billing - hit_tok
            else:
                recent = sum(deltas[max(0, k - 2):k + 1])
                demoted_mass = bases[k] - recent if k >= 3 else 0
                render = RENDER_MERGED if mode == "v3s" else RENDER_CUR
                ctx = demoted_mass * render + recent + TAIL + delta / 2
                billing = s * ctx * calib
                # 轮内首发（实测 3.5M 按轮内容量分摊）+ 每轮一次装配重组
                miss_tok = (REAL_MISS * delta / total_delta
                            + (sum(deltas[max(0, k - 2):k]) + TAIL) * calib)
                hit_tok = max(0.0, billing - miss_tok)
            cum_input += billing
            tot_hit += hit_tok
            tot_miss += miss_tok
            cum_yuan += (hit_tok * HIT + miss_tok * MISS) / 1e6
            # 整理成本（managed 系列，整理调用是全新上下文，按未命中价计）
            if mode != "baseline":
                if mode == "cur":
                    org = ORG_FIXED + ORG_PER_ROUND
                    org_events += 1
                else:
                    fresh += delta
                    if fresh >= WATERMARK:
                        org = ORG_FIXED + fresh * (ORG_PER_ROUND / 25_000)
                        fresh = 0.0
                        org_events += 1
                    else:
                        org = 0.0
                if org:
                    cum_yuan += org / 1e6 * MISS
                    cum_input += org
                    tot_miss += org
            rows.append((k + 1, s, delta, ctx, cum_input, cum_yuan))
        return rows, cum_input, cum_yuan, org_events, tot_hit, tot_miss

    results = {m: simulate(m) for m in ("baseline", "cur", "v3", "v3s")}

    print("逐轮对照（每 4 轮采样，现金为 GLM 半价累计）:")
    print(f"{'轮':>3} {'步':>3} {'净增K':>7} | {'基座K':>7} {'现行ctx':>8} {'狠压ctx':>8} | {'现行¥':>7} {'狠压¥':>7}")
    for i in range(0, n, 4):
        b_row = results["baseline"][0][i]
        c_row = results["cur"][0][i]
        s_row = results["v3s"][0][i]
        print(f"R{i+1:<2} {STEPS[i]:>3} {b_row[2]/1000:>7.1f} | {BASES_K[i]:>7.1f} "
              f"{c_row[3]/1000:>8.0f} {s_row[3]/1000:>8.0f} | "
              f"{c_row[5]:>7.2f} {s_row[5]:>7.2f}")
    print()
    price_books = (
        ("GLM 半价", 0.115, 0.4),
        ("DeepSeek 空闲", 0.05, 1.5),
    )

    def cash_of(res, ph, pm):
        return res[4] / 1e6 * ph + res[5] / 1e6 * pm

    for plabel, ph, pm in price_books:
        print(f"=== 29 轮总账 · {plabel}（命中 {ph} / 未命中 {pm} 元/M）===")
        for m, label in (("baseline", "baseline 对照"), ("cur", "managed 现行"),
                         ("v3", "managed V3水位"), ("v3s", "managed V3狠压")):
            res = results[m]
            _, ci, cy, oe, th, tm = res
            eff = tm + th * (ph / pm)
            print(f"  {label:<16} 输入计费 {ci/1e6:>7.1f}M | 等效 {eff/1e6:>6.2f}M | "
                  f"现金 {cash_of(res, ph, pm):>6.2f} 元 | 命中率 {th/(th+tm):>5.1%} | "
                  f"整理触发 {oe if m != 'baseline' else 0} 次")
        b_cash = cash_of(results["baseline"], ph, pm)
        print(f"  现金成本 baseline/managed = "
              f"{b_cash / cash_of(results['cur'], ph, pm):.2f}x（现行）"
              f" / {b_cash / cash_of(results['v3'], ph, pm):.2f}x（V3水位）"
              f" / {b_cash / cash_of(results['v3s'], ph, pm):.2f}x（V3狠压）")
        print()


if __name__ == "__main__":
    main()
