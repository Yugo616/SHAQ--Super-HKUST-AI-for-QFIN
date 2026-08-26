import fs from "node:fs/promises";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const inputPath = process.argv[2];
const outputPath = process.argv[3];
if (!inputPath || !outputPath) throw new Error("usage: build_campaign_summary.mjs INPUT_JSON OUTPUT_PPTX");
const data = JSON.parse(await fs.readFile(inputPath, "utf8"));

const deck = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const slide = deck.slides.add();
slide.background.fill = "#F7FAFD";

function text(name, value, left, top, width, height, size, color, bold = false, align = "left") {
  const box = slide.shapes.add({
    geometry: "textbox", name,
    position: { left, top, width, height },
    fill: "none", line: { style: "solid", fill: "none", width: 0 },
  });
  box.text = String(value);
  box.text.style = {
    fontFamily: "Arial Unicode MS", fontSize: size, color, bold, alignment: align,
  };
  return box;
}

function rule(name, left, top, width, height, fill) {
  return slide.shapes.add({
    geometry: "rect", name, position: { left, top, width, height },
    fill, line: { style: "solid", fill: "none", width: 0 },
  });
}

text("brand", "SHAQ DAILY ORACLE", 72, 45, 360, 28, 15, "#2F73B7", true);
text("date", `${data.date_start} — ${data.date_end}`, 878, 45, 330, 28, 15, "#65748A", false, "right");
rule("top-rule", 72, 82, 1136, 2, "#C9D8E8");
text("title", "九个交易日，我们留下了什么？", 72, 112, 760, 64, 39, "#102A4C", true);
text("subtitle", "每次预测在盘前冻结；模拟成交、费用和结果随后独立记录。", 74, 183, 920, 38, 21, "#52647B");

const accuracy = data.directional_accuracy == null
  ? "—" : `${(data.directional_accuracy * 100).toFixed(1)}%`;
const netPnl = Number(data.paper_net_pnl || 0);
const pnlText = `${netPnl >= 0 ? "+" : ""}$${netPnl.toFixed(2)}`;
text("runs-number", data.formal_runs, 76, 274, 180, 72, 52, "#1C5D99", true);
text("runs-label", "正式运行", 80, 350, 180, 34, 21, "#52647B", true);
text("accuracy-number", accuracy, 330, 274, 250, 72, 52, "#1C5D99", true);
text("accuracy-label", `方向命中  ${data.correct_predictions}/${data.evaluated_predictions}`, 334, 350, 250, 34, 21, "#52647B", true);
text("pnl-number", pnlText, 650, 274, 250, 72, 52, netPnl >= 0 ? "#16845B" : "#B23A3A", true);
text("pnl-label", "模拟成交净盈亏", 654, 350, 250, 34, 21, "#52647B", true);
text("fees-number", `$${Number(data.fees || 0).toFixed(2)}`, 990, 274, 200, 72, 52, "#1C5D99", true, "right");
text("fees-label", "累计费用", 990, 350, 200, 34, 21, "#52647B", true, "right");

rule("middle-rule", 72, 423, 1136, 2, "#C9D8E8");
text("plain-result", `${data.empty_runs} 次空榜 · ${data.exception_sessions} 次异常记录`, 72, 458, 600, 42, 26, "#102A4C", true);
text("interpretation", "空榜表示没有股票同时满足独立证据门槛；异常记录保留原样，不补做、不回填。", 72, 505, 900, 62, 20, "#52647B");
text("footnote", "这是短期模拟运行记录，不是长期胜率或盈利能力证明。", 72, 638, 900, 28, 16, "#738196");
text("page", "01", 1150, 638, 58, 28, 16, "#738196", false, "right");

const pptx = await PresentationFile.exportPptx(deck);
await pptx.save(outputPath);
