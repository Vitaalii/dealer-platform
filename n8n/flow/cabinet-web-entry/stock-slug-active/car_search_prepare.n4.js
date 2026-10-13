// ==========================================
// ⚙️ НАСТРОЙКИ (Ваш реальный домен NocoDB)
// ==========================================
const MY_NOCO_DOMAIN = 'https://v9dt-o63u-4adx.gw-1a.dockhost.net';
// ==========================================
// [U2] Кредитная математика вынесена в саб-воркфлоу "Tool: Calc Engine".
// Эта нода готовит данные и batch-запрос; результат вливает нода Merge Calc.

// 1. ИНИЦИАЛИЗАЦИЯ И ЗАЩИТА ОТ ПУСТЫХ ДАННЫХ
function getSafeAll(nodeName, checkField) {
    try {
        const items = $(nodeName).all().map(i => i.json);
        if (items.length === 1 && !items[0][checkField]) {
            return [];
        }
        return items;
    } catch (e) { return []; }
}

function getSafeFirst(nodeName, checkField) {
    try {
        const item = $(nodeName).first().json;
        if (!item[checkField]) return null;
        return item;
    } catch (e) { return null; }
}

const triggerNode = $('Execute Workflow Trigger').first();
const triggerData = triggerNode ? triggerNode.json : {};

const context = triggerData.qualification_context || {};
const query = context.model || triggerData.query || "Авто";
const dealerId = context.dealer_id || (triggerData.dealer_id || "unknown");
const userPayment = Number(context.payment || context.credit_payment || triggerData.payment) || 0;
const requestType = context.request_type || "catalog_view";
let targetComplectationId = (context.complectation_id || "00").toString();

const requestTypeMap = {
    "credit": "Расчет кредита",
    "tradein": "Оценка авто в трейд-ин",
    "combo": "Расчет кредита и трейд-ин",
    "stock": "Наличие на складе",
    "consult": "Консультация менеджера"
};
const requestTypeName = requestTypeMap[requestType] || requestType;

let targetComplectationName = (targetComplectationId === "00") ? "Любая" : "Не найдена";

// [STOCK] Каталог — только активные строки (решение владельца 2026-10-05):
// неактивные комплектации («Эксклюзив», «Флагман СЕ» у MONJARO дилера 02)
// попадали в список, который видит Александр.
function isActiveRow(t) {
    const a = t.Active;
    return a === true || a === 1 || a === "1" || a === "true";
}
const catalogItems = getSafeAll('Get Catalog', 'Model').filter(isActiveRow);
const stockItems = getSafeAll('Get Stock', 'Model');
const ratesList = getSafeAll('Get Rates', 'Rate');
// [N4] Программа кредитования дилера. До правки программа выбиралась
// проверкой колонки Program_Name, которой нет ни в одной таблице
// [brand]_Credit_Programs, — найденная строка превращалась в null, и движок
// брал умолчания 84 мес. / 20 %.
//  - признак строки программы — Program_id;
//  - Model_Pattern — список Model id («11, 12, 13»); программа применяется,
//    только если Model id модели стоит в нём ОТДЕЛЬНЫМ элементом (решение
//    владельца 2026-09-30). Прежний фильтр сравнивал с Id строки каталога
//    подстрокой: Id 1 совпадал с «11», модель из списка с первой строкой Id 6
//    программу не получала;
//  - минимальный взнос — ТОЛЬКО Min_Down_Payment, целые проценты (10 = 10 %),
//    во всех трёх таблицах (HAVAL, GEELY, BELGEE — срез 2026-09-30). Пустое
//    значение → 20 %, как в боте (решение владельца 2026-09-30). Движок
//    читает поле Min_Down_Payment_Pct и сам пустое заменил бы на 10 %
//    (calc_engine.js:53), поэтому 20 подставляется здесь явно.
const MIN_DOWN_DEFAULT_PCT = 20;
function pickProgram(rows, modelId) {
    const id = String(modelId == null ? "" : modelId).trim();
    if (!id) return null;
    const row = rows.find(r => r && r.Program_id != null && r.Program_id !== "" &&
        String(r.Model_Pattern || "").split(",").map(s => s.trim()).includes(id));
    if (!row) return null;
    const raw = row.Min_Down_Payment;
    const pct = (raw === undefined || raw === null || String(raw).trim() === "" || isNaN(Number(raw)))
        ? MIN_DOWN_DEFAULT_PCT : Number(raw);
    return Object.assign({}, row, { Min_Down_Payment_Pct: pct });
}
const program = pickProgram(getSafeAll('Get Program', 'Program_id'),
                            catalogItems.length ? catalogItems[0]['Model id'] : null);

if (!catalogItems.length) {
    return {
        json: {
            response: {
                result_text: `По запросу "${query}" модели не найдены в каталоге.`,
                stock_count: 0,
                complectations: []
            },
            calc_request: { operation: "noop" }
        }
    };
}

// ---------------------------------------------------------
// 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ОКРУГЛЕНИЯ
// ---------------------------------------------------------
function round10k(val) { return Math.round(val / 10000) * 10000; }

function extractUrl(fileObj) {
    if (!fileObj) return null;
    if (typeof fileObj === 'string') return fileObj;
    return fileObj.signedUrl || fileObj.url || fileObj.path || null;
}

// ---------------------------------------------------------
// 3. АГРЕГАЦИЯ ДАННЫХ (credit_calculation заполнит Merge Calc)
// ---------------------------------------------------------
const grouped = {};
const calcItems = [];
// [STOCK] slug каталога → комплектация: по нему привязывается сток
const slugToTrim = {};
const trimsWithSlug = new Set();

for (const trim of catalogItems) {
    const trimName = trim.Complectation;
    const trimId = (trim['Complectation id'] || trim['Complectation_id'] || trim.Id || "").toString();
    const trimSlug = String(trim.slug || "").trim().toLowerCase();
    if (trimSlug) {
        if (!(trimSlug in slugToTrim)) slugToTrim[trimSlug] = trimId;
        trimsWithSlug.add(trimId);
    }

    if (trimId === targetComplectationId) {
        targetComplectationName = trimName;
    }

    if (!grouped[trimId]) {
        // --- ЛОГИКА ПЯТИУРОВНЕВОЙ ЦЕНЫ ---
        const msrp = round10k(Number(trim['Price old']) || 0);          // Розничная цена (MSRP)
        const cashBonus = round10k(Number(trim['Cash in hook']) || 0);  // Прямая скидка от салона
        const basePrice = msrp - cashBonus;

        const creditBonus = round10k(Number(trim['Credit bonus']) || 0);
        const tradeinBonus = round10k(Number(trim['Trade-in bonus']) || 0);

        const creditPrice = basePrice - creditBonus;   // Базовая цена минус кредит
        const tradeinPrice = basePrice - tradeinBonus; // Базовая цена минус трейд-ин

        const rawPics = trim['Pics'] || [];
        let picsArray = Array.isArray(rawPics) ? rawPics : [];

        const sortedImages = {};
        for (const pic of picsArray) {
            const fName = (pic.title || pic.path || "").toLowerCase();
            let url = extractUrl(pic);

            if (url && !url.startsWith('http')) {
                const cleanPath = url.startsWith('/') ? url.substring(1) : url;
                url = `${MY_NOCO_DOMAIN}/${cleanPath}`;
            }

            if (url) {
                if (fName.includes('ext1')) sortedImages.exterior_front = url;
                else if (fName.includes('ext2')) sortedImages.exterior_rear = url;
                else if (fName.includes('int1')) sortedImages.interior_dashboard = url;
                else if (fName.includes('int2')) sortedImages.interior_driver = url;
                else if (fName.includes('int3')) sortedImages.interior_door = url;
                else if (fName.includes('int4')) sortedImages.interior_gear = url;
            }
        }

        // [U2] Кредитный расчёт уехал в Calc Engine — собираем batch-заявку
        calcItems.push({ id: trimId, price: creditPrice });

        let featuresList = trim.Features ? trim.Features.split('\n').map(s => s.trim()) : [];
        const minusRaw = trim['Minus Features'] || "";
        const minusList = minusRaw.split('\n').map(s => s.trim().toLowerCase()).filter(s => s !== '');

        const finalFeatures = featuresList.filter(f => {
            if (f === '') return false;
            if (f.toLowerCase().includes('топовые опции')) return false;
            const isForbidden = minusList.some(minus => f.toLowerCase().includes(minus));
            return !isForbidden;
        });

        grouped[trimId] = {
            id: trimId,
            name: trimName,
            full_name: trim['Model title'] || trimName,
            specs: {
                engine: trim.Engine,
                transmission: trim.Transmission_ru || trim.Transmission,
                drive: trim.Drivetrain_ru || trim.Drivetrain,
                acceleration: trim.Acceleration,
                consumption: trim['Сonsumption']
            },
            price: {
                MSRP: msrp,
                cash_bonus: cashBonus,
                base_price: basePrice,
                credit_price: creditPrice,
                credit_bonus: creditBonus,
                tradein_price: tradeinPrice,
                tradein_bonus: tradeinBonus
            },
            features: finalFeatures,
            images: sortedImages,
            stock_count: 0,
            stock_details: { in_stock: 0, in_transit: 0, other: 0 },
            stock_colors: [],
            credit_calculation: null // заполнит Merge Calc
        };
    }
}

// ---------------------------------------------------------
// 4. ПОДСЧЕТ СКЛАДА
// ---------------------------------------------------------
if (stockItems.length > 0) {
    for (const car of stockItems) {
        const carSlug = String(car.slug || "").trim().toLowerCase();
        if (!carSlug) continue;

        // [STOCK] Привязка по точному совпадению slug каталога и slug стока.
        // Прежнее сравнение по названию комплектации не работало для брендов с
        // кириллическими названиями («флагман» ⊄ «flagship-se-2-0t-…», дилер 02)
        // и путало одноимённые комплектации (Elite 2WD / Elite 4WD у HAVAL).
        // Оно оставлено запасным вариантом ТОЛЬКО для комплектаций без slug.
        let matchedTrimKey = slugToTrim[carSlug];
        if (!matchedTrimKey) {
            matchedTrimKey = Object.keys(grouped).find(key => {
                if (trimsWithSlug.has(key)) return false;
                const trimData = grouped[key];
                let searchKey = trimData.name.toLowerCase();
                searchKey = searchKey.replace('+', '-plus').replace(' ', '-');
                return carSlug.includes(searchKey);
            });
        }

        if (matchedTrimKey) {
            const trimData = grouped[matchedTrimKey];
            trimData.stock_count++;
            const status = (car.Status || "").toLowerCase();
            let statusKey = "other";
            if (status.includes("дилер") || status.includes("dealer") || status.includes("stock")) statusKey = "in_stock";
            else if (status.includes("пути") || status.includes("transit") || status.includes("way")) statusKey = "in_transit";

            trimData.stock_details[statusKey]++;

            const colorName = car.Color_ext || "Не указан";
            let colorEntry = trimData.stock_colors.find(c => c.name === colorName);
            if (!colorEntry) {
                colorEntry = { name: colorName, total: 0, in_stock: 0, in_transit: 0 };
                trimData.stock_colors.push(colorEntry);
            }
            colorEntry.total++;
            if (statusKey === 'in_stock') colorEntry.in_stock++;
            if (statusKey === 'in_transit') colorEntry.in_transit++;
        }
    }
}

// ---------------------------------------------------------
// 5. ФОРМИРОВАНИЕ ОТВЕТА (без кредитных расчётов) + BATCH-ЗАЯВКА
// ---------------------------------------------------------
return {
    json: {
        response: {
            user_id: triggerData.user_id,
            session_id: triggerData.session_id,
            chatInput: triggerData.chatInput,
            user_data: triggerData.user_data,
            data_routing: triggerData.data_routing,
            qualification_context: triggerData.qualification_context,

            model: catalogItems[0].Model,
            dealer_id_used: dealerId,
            deal_id: context.deal_id || triggerData.deal_id || null,
            user_request: {
                payment: userPayment,
                query: query,
                type: requestType,
                type_name: requestTypeName,
                selected_complectation_id: targetComplectationId,
                selected_complectation_name: targetComplectationName,
                plate: context.plate || ""
            },
            complectations: Object.values(grouped).sort((a,b) => a.price.MSRP - b.price.MSRP),
            summary: {
                total_stock: stockItems.length,
                model_name: catalogItems[0].Model
            }
        },
        calc_request: {
            operation: "credit_scenarios_batch",
            target_payment: userPayment,
            items: calcItems,
            rates: ratesList,
            program: program
        }
    }
};
