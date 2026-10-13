// ==========================================
// ⚙️ НАСТРОЙКИ (Ваш реальный домен NocoDB)
// ==========================================
const MY_NOCO_DOMAIN = 'https://v9dt-o63u-4adx.gw-1a.dockhost.net';
// ==========================================
// [U2] Комбо-математика вынесена в саб-воркфлоу "Tool: Calc Engine".
// Эта нода готовит данные и batch-запрос; результат вливает нода Merge Calc.

// 1. ИНИЦИАЛИЗАЦИЯ И ЗАЩИТА ОТ ПУСТЫХ ДАННЫХ
function getSafeAll(nodeName, checkField) {
    try {
        const items = $(nodeName).all().map(i => i.json);
        if (items.length === 1 && !items[0][checkField]) return [];
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

// 2. ПЕРЕХВАТ ДАННЫХ ИЗ САБФЛОУ ОЦЕНКИ
let autoEvalData = {};
try {
    autoEvalData = $('Execute Workflow').first().json;
} catch(e) {}

// 🚨 БЛОКИРОВКА №1: ЕСЛИ ОЦЕНКА ВЕРНУЛА ОШИБКУ ИЛИ IN_PROGRESS - СРАЗУ ОТДАЕМ ЕЕ БОТУ
if (autoEvalData && autoEvalData.status && autoEvalData.status !== "SUCCESS") {
    return {
        json: {
            response: {
                combo_calculation: {
                    status: autoEvalData.status, // "IN_PROGRESS" или "NOT_FOUND"
                    message: autoEvalData.message || "Отчет формируется. Уточните марку и год."
                }
            },
            calc_request: { operation: "noop" }
        }
    };
}

// Если оценка SUCCESS, достаем из нее готовые цены и детали
let tradeinValue = autoEvalData.tradein_price_to || 0;
let tradeinStatus = "SUCCESS";
let autoRuCarDetails = {
    tradein_make: autoEvalData.tradein_make || "",
    tradein_model: autoEvalData.tradein_model || "",
    tradein_year: autoEvalData.tradein_year || 0,
    tradein_horse_power: autoEvalData.tradein_horse_power || 0,
    tradein_transmission: autoEvalData.tradein_transmission || "",
    tradein_steering_wheel: autoEvalData.tradein_steering_wheel || ""
};

// 3. СБОР ДАННЫХ ИЗ БАЗЫ (Выполняется только если оценка SUCCESS)
const triggerNode = $('Execute Workflow Trigger').first();
const triggerData = triggerNode ? triggerNode.json : {};

let context = {};
try {
    context = typeof triggerData.qualification_context === 'string'
        ? JSON.parse(triggerData.qualification_context)
        : (triggerData.qualification_context || {});
} catch(e) {}

const query = context.model || triggerData.query || "Авто";
const dealerId = context.dealer_id || (triggerData.dealer_id || "unknown");
const rawPayment = context.payment || context.credit_payment || triggerData.payment || "0";
const userPayment = Number(String(rawPayment).replace(/\D/g, '')) || 0;
const requestType = context.request_type || "combo";
let targetComplectationId = (context.selected_complectation_id || context.complectation_id || "00").toString();
const requestTypeName = "Расчет комбо (Трейд-ин + Кредит)";
let targetComplectationName = (targetComplectationId === "00") ? "Любая" : "Не найдена";

// [STOCK] Каталог — только активные строки (решение владельца 2026-10-05): неактивные
// комплектации («Эксклюзив», «Флагман СЕ» у MONJARO дилера 02) попадали в комбо-расчёт.
// Как в Car Search (car_search_prepare.js).
function isActiveRow(t) {
    const a = t.Active;
    return a === true || a === 1 || a === "1" || a === "true";
}
const catalogItems = getSafeAll('Get Catalog', 'Model').filter(isActiveRow);
const stockItems = getSafeAll('Get Stock', 'Model');
const ratesList = getSafeAll('Get Rates', 'Rate');
const program = getSafeFirst('Get Program', 'Program_Name');

if (!catalogItems.length) {
    return {
        json: {
            response: {
                combo_calculation: {
                    status: "error",
                    message: `По запросу "${query}" модели не найдены в каталоге.`
                }
            },
            calc_request: { operation: "noop" }
        }
    };
}

// ---------------------------------------------------------
// 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ОКРУГЛЕНИЯ
// ---------------------------------------------------------
function round10k(val) { return Math.round(val / 10000) * 10000; }

function extractUrl(fileObj) {
    if (!fileObj) return null;
    if (typeof fileObj === 'string') return fileObj;
    return fileObj.signedUrl || fileObj.url || fileObj.path || null;
}

// tradeinValue сознательно НЕ округляется — живая нода этого не делала
// (round10k был только в удалённом legacy-пути)

// ---------------------------------------------------------
// 5. АГРЕГАЦИЯ ДАННЫХ (combo_calculation заполнит Merge Calc)
// ---------------------------------------------------------
const grouped = {};
const calcItems = [];
let rootCalcId = null; // расчёт какой комплектации отдать в корень ответа

for (const trim of catalogItems) {
    const trimName = trim.Complectation;
    const trimId = (trim['Complectation id'] || trim['Complectation_id'] || trim.Id || "").toString();

    if (trimId === targetComplectationId) targetComplectationName = trimName;

    if (!grouped[trimId]) {
        // [N5] Комбо-цена считается от той же базы, что кредит и трейд-ин в
        // Car Search (car_search_prepare.js: MSRP = Price old, base_price =
        // MSRP − Cash in hook). До правки базой была колонка Price new — в
        // шаблоне бота это «цена с максимальной скидкой» (main.py:2180), —
        // и цена комбо не сходилась с credit_price той же комплектации.
        const msrp = round10k(Number(trim['Price old']) || 0);
        const cashBonus = round10k(Number(trim['Cash in hook']) || 0);
        const basePrice = msrp - cashBonus;
        const creditBonus = round10k(Number(trim['Credit bonus']) || 0);
        const tradeinBonus = round10k(Number(trim['Trade-in bonus']) || 0);

        // Для КОМБО суммируем обе скидки
        const finalComboPrice = basePrice - creditBonus - tradeinBonus;

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
            }
        }

        // [U2] Комбо-расчёт уехал в Calc Engine — собираем batch-заявку
        calcItems.push({ id: trimId, price: finalComboPrice });

        // Логика выбора корневого расчёта сохранена: первая комплектация,
        // либо целевая, если встретилась позже
        if (!rootCalcId || trimId === targetComplectationId) {
            rootCalcId = trimId;
        }

        let featuresList = trim.Features ? trim.Features.split('\n').map(s => s.trim()) : [];
        const minusRaw = trim['Minus Features'] || "";
        const minusList = minusRaw.split('\n').map(s => s.trim().toLowerCase()).filter(s => s !== '');
        const finalFeatures = featuresList.filter(f => {
            if (f === '') return false;
            if (f.toLowerCase().includes('топовые опции')) return false;
            return !minusList.some(minus => f.toLowerCase().includes(minus));
        });

        grouped[trimId] = {
            id: trimId,
            name: trimName,
            full_name: trim['Model title'] || trimName,
            price: {
                MSRP: msrp,
                cash_bonus: cashBonus,
                base_price: basePrice,
                credit_bonus: creditBonus,
                tradein_bonus: tradeinBonus,
                combo_price: finalComboPrice
            },
            features: finalFeatures,
            images: sortedImages,
            combo_calculation: null // заполнит Merge Calc
        };
    }
}

// ---------------------------------------------------------
// 6. ФОРМИРОВАНИЕ ОТВЕТА (без комбо-расчётов) + BATCH-ЗАЯВКА
// ---------------------------------------------------------
return {
    json: {
        response: {
            model: catalogItems[0].Model,
            dealer_id_used: dealerId,
            user_request: {
                payment: userPayment,
                query: query,
                type: requestType,
                type_name: requestTypeName,
                selected_complectation_id: targetComplectationId,
                selected_complectation_name: targetComplectationName,
                plate: context.plate || triggerData.plate || ""
            },
            tradein_evaluation: {
                status: tradeinStatus,
                estimated_value: tradeinValue,
                ...autoRuCarDetails,
                // [U4] Сырой found_info (уже с вложенным госномером — его кладёт
                // воркфлоу оценки) для личного кабинета: Final Response достаёт его
                // отсюда в json_cache. Ключ добавляется только когда блок реально
                // пришёл — контракт кабинета «ключей просто нет», а не пустой объект.
                ...(autoEvalData.found_info && Object.keys(autoEvalData.found_info).length
                    ? { found_info: autoEvalData.found_info } : {})
            },
            combo_calculation: null, // заполнит Merge Calc (расчёт rootCalcId)
            complectations: Object.values(grouped).sort((a,b) => a.price.MSRP - b.price.MSRP),
            summary: {
                total_stock: stockItems.length,
                model_name: catalogItems[0].Model
            }
        },
        root_calc_id: rootCalcId,
        calc_request: {
            operation: "combo_scenarios_batch",
            target_payment: userPayment,
            tradein_value: tradeinValue,
            tradein_status: tradeinStatus,
            items: calcItems,
            rates: ratesList,
            program: program
        }
    }
};
