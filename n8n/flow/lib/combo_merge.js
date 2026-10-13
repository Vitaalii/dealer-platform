// [U2] Вливает результаты Calc Engine в подготовленный ответ Combo Calculator
const prep = $('Prepare Combo').first().json;
const calc = $input.first().json;
const response = prep.response;

if (calc && calc.results) {
    for (const comp of (response.complectations || [])) {
        if (calc.results[comp.id] !== undefined) {
            comp.combo_calculation = calc.results[comp.id];
        }
    }
    if (prep.root_calc_id && calc.results[prep.root_calc_id] !== undefined) {
        response.combo_calculation = calc.results[prep.root_calc_id];
    }
}

return { json: response };
