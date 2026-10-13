// [U2] Вливает результаты Calc Engine в подготовленный ответ Car Search
const prep = $('Prepare Data').first().json;
const calc = $input.first().json;
const response = prep.response;

if (calc && calc.results) {
    for (const comp of (response.complectations || [])) {
        if (calc.results[comp.id] !== undefined) {
            comp.credit_calculation = calc.results[comp.id];
        }
    }
}

return { json: response };
