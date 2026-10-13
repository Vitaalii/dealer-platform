// ============================================================
// N2: подилерные файлы эталона в формате бота.
//
// Вход:  price_reference.json (build_price_reference.mjs — код нод на живых
//        каталогах) + раскладка «дилер → каталог» JSON-объектом
//        {"02": {"catalog": "m2flt4d2r5d9cpa", "active_field": "Active_1"}, "09": "m2flt4d2r5d9cpa", …}
//        (значение — catalog_table_id строкой или объект с колонкой активности дилера;
//        нет active_field — "Active", как в n8n при отсутствии data_routing.catalog_active_field).
// Выход: dealers/dealer_<code>.json
//   {"dealer_code", "generated_at", "source", "positions": [
//       {"model_id", "complectation_id", "credit_price", "combo_price"}]}
//   model_id / complectation_id — строки; цены — целые рубли;
//   combo_price — формула N5 (base_price − credit_bonus − tradein_bonus).
// В positions НЕ входят: строки из ignored_row_ids (их нет в эталоне по
// построению — Car Search берёт первую строку комплектации), позиции без
// Model id и позиции с credit_price <= 0 (DATA_ISSUES.md, пп. 1–2).
//
// Запуск из корня репозитория:
//   node flow/cabinet-web-entry/n2-price-reference/build_dealer_files.mjs <раскладка.json>
// ============================================================
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

export const SOURCE = "n8n: Car Search + Combo Calculator";

// ISO-время в часовом поясе Москвы (+03:00, без перехода на летнее время).
export function moscowIso(iso) {
    const t = new Date(new Date(iso).getTime() + 3 * 3600 * 1000);
    return t.toISOString().replace(/\.\d{3}Z$/, "+03:00");
}

export function dealerFile(reference, dealerCode, mappingValue) {
    const catalogTableId = typeof mappingValue === "string" ? mappingValue : mappingValue.catalog;
    const wanted = (typeof mappingValue === "object" && mappingValue.active_field) || "Active";
    const fields = (reference.positions_by_active_field || {})[catalogTableId];
    if (fields) return fromActiveField(reference, dealerCode, catalogTableId, fields[wanted] ? wanted : "Active");
    const rows = reference.rows.filter(r => r.catalog_table_id === catalogTableId);
    if (!rows.length) {
        const known = [...new Set(reference.rows.map(r => `${r.catalog_table_id} (${r.catalog_title})`))];
        throw new Error(`дилер ${dealerCode}: каталога ${catalogTableId} нет в эталоне; есть: ${known.join(", ")}`);
    }
    const excluded = [];
    const positions = [];
    for (const r of rows) {
        const modelId = r.model_id == null ? "" : String(r.model_id).trim();
        if (!modelId) { excluded.push({ row_id: r.row_id, reason: "нет Model id" }); continue; }
        if (!(r.price.credit_price > 0) || !(r.combo_price.n5 > 0)) {
            excluded.push({ row_id: r.row_id, reason: "цена <= 0 (нет Price old)" });
            continue;
        }
        positions.push({
            model_id: modelId,
            complectation_id: String(r.complectation_id),
            credit_price: Math.round(r.price.credit_price),
            combo_price: Math.round(r.combo_price.n5),
        });
    }
    const n = s => (/^\d+$/.test(s) ? Number(s) : Infinity);
    positions.sort((a, b) => n(a.model_id) - n(b.model_id) || a.model_id.localeCompare(b.model_id)
        || n(a.complectation_id) - n(b.complectation_id));
    return {
        file: { dealer_code: String(dealerCode), generated_at: moscowIso(reference.generated_at), source: SOURCE, positions },
        excluded,
    };
}

// Позиции текущего кода нод с колонкой активности дилера (positions_by_active_field).
function fromActiveField(reference, dealerCode, catalogTableId, field) {
    const excluded = [];
    const positions = [];
    for (const p of reference.positions_by_active_field[catalogTableId][field]) {
        const modelId = p.model_id == null ? "" : String(p.model_id).trim();
        if (!modelId) { excluded.push({ row_id: p.row_id, reason: "нет Model id" }); continue; }
        if (!(p.credit_price > 0) || !(p.combo_price > 0)) { excluded.push({ row_id: p.row_id, reason: "цена <= 0 (нет Price old)" }); continue; }
        positions.push({ model_id: modelId, complectation_id: String(p.complectation_id),
                         credit_price: Math.round(p.credit_price), combo_price: Math.round(p.combo_price) });
    }
    const n = s => (/^\d+$/.test(s) ? Number(s) : Infinity);
    positions.sort((a, b) => n(a.model_id) - n(b.model_id) || a.model_id.localeCompare(b.model_id)
        || n(a.complectation_id) - n(b.complectation_id));
    return { file: { dealer_code: String(dealerCode), generated_at: moscowIso(reference.generated_at), source: SOURCE, positions },
             excluded, active_field: field };
}

if (import.meta.url === `file://${process.argv[1]}`) {
    const mappingPath = process.argv[2];
    if (!mappingPath) {
        console.error("usage: build_dealer_files.mjs <раскладка дилер→каталог.json>");
        process.exit(2);
    }
    const reference = JSON.parse(readFileSync(new URL("price_reference.json", import.meta.url), "utf-8"));
    const mapping = JSON.parse(readFileSync(mappingPath, "utf-8"));
    const outDir = new URL("dealers/", import.meta.url);
    mkdirSync(outDir, { recursive: true });
    for (const [code, catalog] of Object.entries(mapping)) {
        const { file, excluded, active_field } = dealerFile(reference, code, catalog);
        writeFileSync(new URL(`dealer_${code}.json`, outDir), JSON.stringify(file, null, 2) + "\n");
        console.log(`dealer_${code}.json: колонка ${active_field || "—"}, ${file.positions.length} позиций; исключено ${excluded.length}` +
            (excluded.length ? ` (${excluded.map(e => `Id ${e.row_id}: ${e.reason}`).join("; ")})` : ""));
    }
}
