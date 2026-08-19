// RIVET 品类调研可视化面板（2026-08 网络调研）。
// 在 Codex IDE 的 Canvas 面板打开此文件查看；数据与
// docs/research/market-scan-report-2026-08.md 一致（厂商数据为自报口径）。
import {
  Callout,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Row,
  Stack,
  Stat,
  Table,
  Text,
} from "cursor/canvas";

function MarketSignals() {
  return (
    <Stack gap={8}>
      <H2>一、市场关键信号（2025–2026）</H2>
      <Table
        headers={["信号", "事实", "时间"]}
        columnAlign={["left", "left", "right"]}
        rows={[
          [
            "计费范式转向 per-resolution",
            "Fin / Zendesk 按自动化解决计费；Salesforce Agentforce 按会话 + Flex Credits",
            "2025",
          ],
          [
            "行业整合",
            "Salesforce $3.6B 收购 Fin（原 Intercom）；Zendesk 收购 Forethought",
            "2026-03/06",
          ],
          [
            "国内政策窗口",
            "淘宝/京东/拼多多/抖音/快手全面取消「仅退款」，商家自主售后",
            "2025-04",
          ],
          [
            "大厂免费抢入口",
            "京小智 5.0 免费（618 超百万商家）；AI 店小蜜转人工率 -45%、转化 +10%（自报）",
            "2025-09/2026-05",
          ],
          [
            "行业治理数据差",
            "79% 组织无 Agent 护栏；Salesforce 研究称 LLM agent 65% CX 任务失败；Gartner：2027 年 40% agentic 项目失败",
            "2025",
          ],
          [
            "合规与风控变量",
            "《AI 生成合成内容标识办法》印发；PIPL 最小必要；淘宝天猫上线售后 AI 假图识别",
            "2025-03/2026-04",
          ],
          [
            "头部指标叙事",
            "独立解决率 80–91%（厂商自报）；Klarna 首月 AI 处理 2/3 会话",
            "2024–2026",
          ],
        ]}
      />
      <Text tone="tertiary" size="small">
        来源：三路网络调研（59+ 次检索，2026-08）；厂商自报口径已标注。
      </Text>
    </Stack>
  );
}

function Comparison() {
  return (
    <Stack gap={8}>
      <H2>二、RIVET 对标：已领先 vs 差距</H2>
      <Grid columns={2} gap={16}>
        <Stack gap={8}>
          <H3>已被行业验证的设计（保持）</H3>
          <Table
            framed
            headers={["RIVET 现状", "行业印证"]}
            columnAlign={["left", "left"]}
            rowTone={[
              "success",
              "success",
              "success",
              "success",
              "success",
              "success",
              "success",
              "success",
            ]}
            rows={[
              ["单 Agent 精确 9 工具", "Anthropic：单 Agent 处理绝大多数企业工作流"],
              ["prepare→宿主展示→确认→execute", "同构 Fin Procedures / Cloudflare HITL（运行时权威）"],
              ["认证在 Agent 外 + 跨客户隔离", "行业方向：agent 只带会话令牌，宿主做认证授权"],
              ["工具白名单 + extra=forbid", "对齐 OWASP LLM06 过度代理、最小权限"],
              ["attempt 级预算预扣闸门（¥20）", "优于行业「事后账单」；LLM10 无界消耗正确解法"],
              ["版本化政策 + 确定性规则", "对齐「结构化执行优先于向量 RAG」"],
              ["holdout/校准/回归 + 原子命题裁判", "对齐 LangSmith regression、Decagon 评测引擎"],
              ["服务端幂等重放", "对齐退款接口标配"],
            ]}
          />
        </Stack>
        <Stack gap={8}>
          <H3>与市场实践的差距（建议范围）</H3>
          <Table
            framed
            headers={["差距", "项目现状（已核实）"]}
            columnAlign={["left", "left"]}
            rowTone={[
              "warning",
              "warning",
              "warning",
              "warning",
              "warning",
              "warning",
              "warning",
              "warning",
            ]}
            rows={[
              [
                "转人工闭环缺失",
                "create_handoff_ticket 契约/服务/路由已存在，但未接入任何 Agent 路径与 demo",
              ],
              [
                "指标叙事单一",
                "只有 pass^k；缺 resolution / deflection / handoff 口径与成本曲线",
              ],
              [
                "护栏只有全局一层",
                "缺 per-conversation 软闸门、per-action 上限、模型路由",
              ],
              [
                "审计粒度不足",
                "缺决策快照（规则/政策版本、成本、确认人）与已执行动作撤销",
              ],
              ["无多模态验货接口", "行业已普及凭证识别/假图识别，项目无 mock 占位"],
              ["合规叙事空白", "缺 AI 标识、PIPL 最小必要、PCI 边界声明与数据流图"],
              ["缺 shadow/A-B 评测通道", "只有正式付费评测，无离线回放成本/风险报告"],
              ["回答不强制引用", "政策返回版本化文本，最终回答不强制引用条款"],
            ]}
          />
        </Stack>
      </Grid>
    </Stack>
  );
}

function Recommendations() {
  return (
    <Stack gap={8}>
      <H2>三、分级改进建议（按优先级）</H2>
      <Table
        headers={["优先级", "建议", "落地模块", "工作量"]}
        columnAlign={["left", "left", "left", "right"]}
        rows={[
          ["P0", "README 增「行业对标」章节：per-resolution 成本叙事 + Air Canada 案 + 仅退款退潮背景", "README / docs", "0.5d"],
          ["P0", "「为什么单 Agent + 确定性后端、不用 RAG」决策文档（引 Anthropic 判据）", "docs（新文件）", "0.5d"],
          ["P0", "指标三件套：用已有 run 数据补 resolution/deflection/handoff 口径与每任务成本（不重跑）", "README / evals / docs", "1d"],
          ["P0", "合规最小实现：AI 生成标识 + PIPL/PCI 声明 + 数据流图", "前端 / README", "0.5d"],
          ["P1", "转人工闭环接线：reject/预算耗尽/补槽失败 → 落 SupportTicket（Agent 保持 9 工具）", "app/demo/host.py", "1d"],
          ["P1", "护栏分层：per-conversation 软闸门 + 模型路由预留（¥20 全局硬上限不变）", "app/agent/deepseek_budget.py", "1–2d"],
          ["P1", "决策审计快照落库（规则/政策版本、资格输入、成本、确认人）", "app/models.py / services", "1–2d"],
          ["P1", "幂等再加两层：DB 唯一约束 + 状态机跃迁矩阵硬校验", "models / domain/state_machine", "1d"],
          ["P1", "凭证校验 mock 工具（宿主侧，不进 Agent allowlist）", "app/tools/contracts.py", "0.5d"],
          ["P1", "shadow 模式评测：离线回放公开回归集，产出全自动成本/风险报告（零付费）", "evals（新 runner）", "1d"],
          ["P1", "回答强制引用政策条款/版本，信息不足显式拒答转人工", "agent prompts / 回归集", "0.5d"],
          ["P2", "新 holdout 题集（新 case_set + 重绑校准，按既有协议）", "evals", "需授权"],
          ["P2", "工具轨迹可视化回放器（ToolTrace 已落库）", "app/static", "1–2d"],
          ["P2", "政策版本 × 评测联动演示（v1→v2 行为变化）", "evals / policies", "1d"],
        ]}
      />
      <Text tone="tertiary" size="small">
        约束：遵守 AGENTS.md（单 Agent、不引入多 Agent/LangGraph/MCP；¥20 硬上限；holdout v1/v2 禁止重跑）。
      </Text>
    </Stack>
  );
}

export default function RivetMarketScan() {
  return (
    <Stack gap={20} style={{ padding: 24 }}>
      <Stack gap={4}>
        <H1>RIVET 客服 Agent · 品类调研与改进建议</H1>
        <Text tone="secondary">
          2026-08 · 三路并行调研（海外企业级 / 国内市场 / 工程与评测实践）· 59+ 次检索 · 厂商自报数据均已标注
        </Text>
      </Stack>

      <Row gap={16}>
        <Stat value="59+" label="网络检索次数" />
        <Stat value="67–91%" label="厂商自报独立解决率区间" tone="info" />
        <Stat value="79%" label="组织无 Agent 护栏（Amla 2025）" tone="warning" />
        <Stat value="$3.6B" label="Salesforce 收购 Fin（2026-06）" />
      </Row>

      <MarketSignals />
      <Divider />
      <Comparison />
      <Divider />
      <Recommendations />

      <Callout tone="neutral" title="明确不做（守住架构边界）">
        多 Agent / LangGraph / MCP / 完整 Eval 框架；真实支付与 PCI 落地；PostgreSQL（属 Phase 5
        既有计划）；重跑 holdout v1/v2。
      </Callout>
      <Callout tone="info" title="落地节奏">
        先 P0 后 P1，投入约一周。短板不在架构而在收口：转人工接线、指标口径翻译、合规叙事、护栏分层与审计快照。
      </Callout>
      <Text tone="tertiary" size="small">
        来源：三路调研子代理（2026-08），完整来源链接见《RIVET品类调研与改进建议_2026-08.md》。
      </Text>
    </Stack>
  );
}
