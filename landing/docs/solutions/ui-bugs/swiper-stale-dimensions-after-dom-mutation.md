---
title: Swiper не обновляется после $.html() — стале размеры слайдов
date: 2026-06-15
category: ui-bugs
module: gallery
problem_type: ui_bug
component: frontend_stimulus
symptoms:
  - Swiper-галерея показывает первоначальный набор слайдов и не обновляется при смене данных
  - Навигация по слайдам работает, но отображаются старые изображения
  - Свипер переинициализируется без ошибок в консоли, но DOM визуально не меняется
root_cause: async_timing
resolution_type: code_fix
severity: medium
tags:
  - swiper
  - dom-reflow
  - settimeout
  - gallery
  - initialization-timing
  - tilda
---

# Swiper не обновляется после $.html() — стале размеры слайдов

## Problem

Swiper-галерея инициализировалась один раз при загрузке страницы и больше не обновляла содержимое при смене данных (модель/модификация/комплектация автомобиля). `destroy(true, true)` + `$.html(newSlides)` + `new Swiper()` вызывались синхронно в одной функции — визуально слайды оставались прежними.

## Symptoms

- Галерея показывает первый набор фотографий и не меняет их при смене комплектации
- `console.log` подтверждает: `updateGallery(car)` вызывается с корректным новым `car`, `galleryUrls` пересчитываются правильно
- `.swiper-wrapper` получает новый HTML (новые `<img>` теги в DOM), но Swiper позиционирует враппер на старой позиции
- Swiper не бросает ошибок при destroy и повторной инициализации

## What Didn't Work

- Подозрение на Swiper `destroy(true, true)` с тихим `catch(e){}` — ошибок не было, destroy отрабатывал корректно
- Подозрение на неверные данные в замыкании `forEach` — проверено через логи, `car` передаётся правильный
- Подозрение на кэшированные `galleryUrls` — проверено через логи, массив каждый раз пересчитывается заново

## Solution

Заменить синхронный `new Swiper()` на `setTimeout(fn, 0)` с генерационным счётчиком для защиты от rapid-click:

**До:**
```js
// В updateGallery():
if (gallerySwiper) {
    try { gallerySwiper.destroy(true, true); } catch (e) {}
    gallerySwiper = null;
}
$('#uc-gallery-custom .swiper-wrapper').html(slidesHtml);
$('#uc-gallery-custom').show();

var loop = galleryUrls.length > 1;
gallerySwiper = new Swiper('#uc-gallery-custom .swiper', { loop: loop, ... }); // ← синхронно
```

**После:**
```js
// В IIFE-переменных:
var _swiperGen = 0;

// В updateGallery():
if (gallerySwiper) {
    try { gallerySwiper.destroy(true, true); } catch (e) {}
    gallerySwiper = null;
}
$('#uc-gallery-custom .swiper-wrapper').html(slidesHtml);
$('#uc-gallery-custom').show();

var loop = galleryUrls.length > 1;
var gen  = ++_swiperGen;
setTimeout(function () {
    if (gen !== _swiperGen) return; // вызов устарел — запущен более новый
    gallerySwiper = new Swiper('#uc-gallery-custom .swiper', { loop: loop, ... });
}, 0);
```

## Why This Works

`$.html()` мутирует DOM и ставит browser reflow в очередь — он не выполняется немедленно. Если `new Swiper()` вызывается в том же синхронном тике, Swiper читает `el.offsetWidth` и размеры слайдов до завершения reflow и получает нулевые или устаревшие значения.

Swiper с `loop: true`, `centeredSlides: true` и `slidesPerView: 1.35` критически зависит от корректных измерений: он вычисляет transform для враппера и позиции loop-клонов. При неверных размерах transform позиционирует враппер на позицию старого инстанса — визуально слайды не меняются.

`setTimeout(fn, 0)` переносит `new Swiper()` в следующую задачу event loop. К этому моменту браузер завершил reflow, размеры корректны, и Swiper правильно позиционирует первый слайд нового набора.

Генерационный счётчик `_swiperGen` решает race condition при быстром клике: если `updateGallery` вызвана повторно до срабатывания предыдущего `setTimeout`, устаревший колбэк видит `gen !== _swiperGen` и выходит без создания лишнего инстанса.

## Prevention

- При использовании Swiper с `loop`, `centeredSlides`, или `slidesPerView > 1` — всегда инициализировать через `setTimeout(fn, 0)` после любой DOM-мутации контента слайдов
- Паттерн destroy + `$.html()` + немедленный `new Library()` ненадёжен для всех библиотек, которые читают layout при инициализации (Swiper, Splide, Flickity, EmblaCarousel и аналоги)
- При отладке "не обновляется" — первым делом добавить `console.log(galleryUrls)` после нормализации: если URLs корректные, а галерея не меняется — проблема в timing инициализации

## Related Issues

- `docs/tilda-blocks/u6-gallery.html` — исходный файл с реализацией галереи
- `docs/tilda-blocks/u3-card-sliders.html` — вызывает `window.updateGallery(car)` из `selectComplectation()`
