import { openPDFSource, CHUNK_SIZE, prefetchPages } from './pdf-range.js'
import { unionBoxes, fullBox, darkTheme, imageBoxes, imageBlendDelta, imageModeBlend } from './pdf-appearance.js?v=20261019-1'
const pdfjsPath = path => new URL(`vendor/pdfjs/${path}`, import.meta.url).toString()

// Менять при обновлении PDF.js или compat, вместе с импортами compat.
export const PDFJS_BUILD = '20261016-1'
import './vendor/pdfjs/pdf.mjs?v=20261016-1'
const pdfjsLib = globalThis.pdfjsLib
pdfjsLib.GlobalWorkerOptions.workerSrc = pdfjsPath(`pdf.worker.mjs?v=${PDFJS_BUILD}`)

const fetchText = async url => await (await fetch(url)).text()

// https://raw.githubusercontent.com/mozilla/pdf.js/refs/tags/v5.5.207/web/text_layer_builder.css
const textLayerBuilderCSS = await fetchText(pdfjsPath('text_layer_builder.css'))

// https://raw.githubusercontent.com/mozilla/pdf.js/refs/tags/v5.5.207/web/annotation_layer_builder.css
const annotationLayerBuilderCSS = await fetchText(pdfjsPath('annotation_layer_builder.css'))

const render = async (page, doc, zoom, box, options) => {
    doc.body.style.background = options.theme === 'day' ? 'white' : (options.bg || 'white')
    const scale = zoom * devicePixelRatio
    doc.documentElement.style.transform = `scale(${1 / devicePixelRatio})`
    doc.documentElement.style.transformOrigin = 'top left'
    doc.documentElement.style.setProperty('--scale-factor', scale)
    const viewport = page.getViewport({ scale })
    // Явная подложка нужна для смешивания canvas, включая пустые страницы.
    doc.body.style.width = `${viewport.width}px`
    doc.body.style.height = `${viewport.height}px`
    doc.body.style.isolation = 'isolate'
    const layers = doc.querySelector('#layers')
    layers.style.position = 'absolute'
    layers.style.left = `${-box.left * viewport.width}px`
    layers.style.top = `${-box.top * viewport.height}px`
    doc.documentElement.style.overflow = 'hidden'
    doc.querySelector('.textLayer').replaceChildren()
    doc.querySelector('.annotationLayer').replaceChildren()

    // the canvas must be in the `PDFDocument`'s `ownerDocument`
    // (`globalThis.document` by default); that's where the fonts are loaded
    const canvas = document.createElement('canvas')
    canvas.height = viewport.height
    canvas.width = viewport.width
    let pageImage = canvas
    const canvasContext = canvas.getContext('2d')
    await page.render({ canvasContext, viewport }).promise
    doc.querySelector('#image-original')?.remove()
    doc.imageWheelHandler = null
    if (darkTheme(options.theme) || ['sepia', 'gray', 'grey'].includes(options.theme)) {
        const parseColor = value => {
            const ctx = document.createElement('canvas').getContext('2d')
            ctx.fillStyle = value
            ctx.fillRect(0, 0, 1, 1)
            return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3)
        }
        const dark = darkTheme(options.theme)
        const colors = { dark, bg: parseColor(options.bg || '#ffffff'), fg: parseColor(options.fg || '#dddddd') }
        const pixels = canvasContext.getImageData(0, 0, canvas.width, canvas.height)
        const boxes = dark ? imageBoxes(await page.getOperatorList(), pdfjsLib.OPS, viewport.transform) : []
        // Однобайтная маска позволяет перекрасить текст и картинки за один проход.
        const mask = boxes.length ? new Uint8Array(canvas.width * canvas.height) : null
        for (const b of boxes) {
            const left = Math.max(0, Math.floor(b.left)), right = Math.min(canvas.width, Math.ceil(b.right))
            if (left >= right) continue
            for (let y = Math.max(0, Math.floor(b.top)); y < Math.min(canvas.height, Math.ceil(b.bottom)); y++)
                mask.fill(1, y * canvas.width + left, y * canvas.width + right)
        }
        // Та же формула, что mapPixel, но без массивов на пиксель: на hi-DPI
        // странице миллионы пикселей, и аллокации растягивали рендер до секунд.
        const d = pixels.data, [br, bgc, bb] = colors.bg, [fr, fgc, fb] = colors.fg
        for (let i = 0; i < d.length; i += 4) {
            const r = d[i], g = d[i + 1], b = d[i + 2]
            if (!dark) { d[i] = r * br / 255; d[i + 1] = g * bgc / 255; d[i + 2] = b * bb / 255; continue }
            if (mask && mask[i >>> 2]) {
                const light = (r * .2126 + g * .7152 + b * .0722) / 255
                d[i] = br * light + fr * (1-light)
                d[i+1] = bgc * light + fgc * (1-light)
                d[i+2] = bb * light + fb * (1-light)
                continue
            }
            const hi = r > g ? (r > b ? r : b) : (g > b ? g : b)
            const lo = r < g ? (r < b ? r : b) : (g < b ? g : b)
            const keep = hi ? Math.min(1, (hi - lo) / hi * 2) : 0, light = (hi + lo) / 510, k = 1 - keep
            d[i] = keep * r + k * (fr * (1 - light) + br * light)
            d[i + 1] = keep * g + k * (fgc * (1 - light) + bgc * light)
            d[i + 2] = keep * b + k * (fb * (1 - light) + bb * light)
        }
        if (dark && boxes.length) {
            const overlay = doc.createElement('canvas')
            overlay.id = 'image-original'
            overlay.width = canvas.width; overlay.height = canvas.height
            Object.assign(overlay.style, { position: 'absolute', left: '0', top: '0', pointerEvents: 'none' })
            const ctx = overlay.getContext('2d')
            // Сохраняем оригинал через canvas: без ещё одного RGBA-буфера и записи по пикселям.
            ctx.beginPath()
            for (const b of boxes) ctx.rect(b.left, b.top, b.right-b.left, b.bottom-b.top)
            ctx.clip()
            ctx.drawImage(canvas, 0, 0)
            overlay.style.opacity = options.getImageBlend()
            layers.append(overlay)
            // Capture раньше обработчиков листания и ленты; координаты учитывают crop и DPI.
            doc.imageWheelHandler = event => {
                const delta = event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX
                if (event.ctrlKey || !delta || (!event.shiftKey && Math.abs(event.deltaY) > Math.abs(delta))) return
                const rect = pageImage.getBoundingClientRect()
                const x = (event.clientX - rect.left) * pageImage.width / rect.width
                const y = (event.clientY - rect.top) * pageImage.height / rect.height
                if (!boxes.some(b => x >= b.left && x < b.right && y >= b.top && y < b.bottom)) return
                event.preventDefault(); event.stopImmediatePropagation()
                options.setImageBlend(imageBlendDelta(options.getImageBlend(), delta, event.deltaMode, doc.defaultView.innerWidth))
                let hint = doc.querySelector('#image-hint')
                if (!hint) {
                    hint = doc.createElement('div'); hint.id = 'image-hint'
                    Object.assign(hint.style, {position:'fixed', left:'16px', top:'16px', padding:'8px', background:options.bg, color:options.fg, zIndex:'100', pointerEvents:'none'})
                    doc.body.append(hint)
                }
                hint.style.background = options.bg
                hint.style.color = options.fg
                hint.style.fontSize = `${14 * devicePixelRatio}px`
                hint.textContent = `Картинки: ${Math.round(options.getImageBlend() * 100)}%`
                hint.hidden = false
                clearTimeout(doc.imageHintTimer)
                doc.imageHintTimer = setTimeout(() => { hint.hidden = true }, 1000)
            }
            // Обработчик заменяется при масштабе; перехват устанавливается один раз.
            if (!doc.imageWheelAttached) {
                doc.addEventListener('wheel', event => doc.imageWheelHandler?.(event), {capture:true, passive:false})
                doc.imageWheelAttached = true
            }
        }
        canvasContext.putImageData(pixels, 0, 0)
    }
    // Оригинал для оверлея уже скопирован, тема применена: убираем живой canvas.
    const blob = await new Promise(resolve => canvas.toBlob(resolve))
    let imageURL
    if (blob) {
        imageURL = URL.createObjectURL(blob)
        const img = doc.createElement('img')
        img.width = canvas.width; img.height = canvas.height
        img.style.cssText = `display:block;width:${canvas.width}px;height:${canvas.height}px`
        img.src = imageURL
        try {
            await img.decode()
            pageImage = img
        } catch (error) {
            // Картинка не декодировалась — показываем страницу живым canvas, как раньше.
            console.warn(`PDF: страница ${page.pageNumber} остаётся canvas, decode не удался:`, error)
            URL.revokeObjectURL(imageURL)
            imageURL = undefined
        }
    } else console.warn(`PDF: toBlob вернул null, страница ${page.pageNumber}`)
    const previousURL = doc.pdfImageURL
    doc.querySelector('#canvas').replaceChildren(doc.adoptNode(pageImage))
    doc.pdfImageURL = imageURL
    if (previousURL) URL.revokeObjectURL(previousURL)
    if (pageImage !== canvas) { canvas.width = 0; canvas.height = 0 }

    const container = doc.querySelector('.textLayer')
    const textLayer = new pdfjsLib.TextLayer({
        textContentSource: await page.streamTextContent(),
        container, viewport,
    })
    await textLayer.render()

    // hide "offscreen" canvases appended to docuemnt when rendering text layer
    // https://github.com/mozilla/pdf.js/blob/642b9a5ae67ef642b9a8808fd9efd447e8c350e2/web/pdf_viewer.css#L51-L58
    for (const canvas of document.querySelectorAll('.hiddenCanvasElement'))
        Object.assign(canvas.style, {
            position: 'absolute',
            top: '0',
            left: '0',
            width: '0',
            height: '0',
            display: 'none',
        })

    // fix text selection
    // https://github.com/mozilla/pdf.js/blob/642b9a5ae67ef642b9a8808fd9efd447e8c350e2/web/text_layer_builder.js#L105-L107
    const endOfContent = document.createElement('div')
    endOfContent.className = 'endOfContent'
    container.append(endOfContent)
    // TODO: this only works in Firefox; see https://github.com/mozilla/pdf.js/pull/17923
    container.onpointerdown = () => container.classList.add('selecting')
    container.onpointerup = () => container.classList.remove('selecting')

    const div = doc.querySelector('.annotationLayer')
    const linkService = {
        goToDestination: () => {},
        getDestinationHash: dest => JSON.stringify(dest),
        addLinkAttributes: (link, url) => link.href = url,
    }
    await new pdfjsLib.AnnotationLayer({ page, viewport, div, linkService })
        .render({ annotations: await page.getAnnotations() })
}

const renderPage = async (page, getImageBlob, getBox = () => fullBox(), getOptions = () => ({})) => {
    const viewport = page.getViewport({ scale: 1 })
    if (getImageBlob) {
        const canvas = document.createElement('canvas')
        canvas.height = viewport.height
        canvas.width = viewport.width
        const canvasContext = canvas.getContext('2d')
        await page.render({ canvasContext, viewport }).promise
        return new Promise(resolve => canvas.toBlob(resolve))
    }
    const src = URL.createObjectURL(new Blob([`
        <!DOCTYPE html>
        <html lang="en">
        <meta charset="utf-8">
        <meta name="viewport" content="width=${viewport.width}, height=${viewport.height}">
        <style>
        html, body {
            margin: 0;
            padding: 0;
        }
        /*
        https://github.com/mozilla/pdf.js/commit/bd05b255fabfc313b194bfe9a17ccded4d90fb5a
        */
        :root {
          --user-unit: 1;
          --total-scale-factor: calc(var(--scale-factor) * var(--user-unit));
          --scale-round-x: 1px;
          --scale-round-y: 1px;
        }
        ${textLayerBuilderCSS}
        ${annotationLayerBuilderCSS}
        </style>
        <div id="layers"><div id="canvas"></div>
        <div class="textLayer"></div>
        <div class="annotationLayer"></div></div>
    `], { type: 'text/html' }))
    // Один рендер на документ одновременно: ResizeObserver не должен запускать
    // конкурирующие TextLayer и оставлять старый масштаб поверх нового.
    const renders = new WeakMap()
    const onZoom = ({ doc, scale }) => {
        const box = getBox(), options = getOptions()
        const key = JSON.stringify([scale, box, options])
        const previous = renders.get(doc)
        if (previous?.key === key) return previous.pending
        const pending = (previous?.pending ?? Promise.resolve()).catch(() => {})
            .then(() => render(page, doc, scale, box, options))
        const state = { key, pending }
        renders.set(doc, state)
        pending.catch(() => { if (renders.get(doc) === state) renders.delete(doc) })
        return pending
    }
    const getSize = () => {
        const b = getBox()
        return { width: viewport.width * (b.right - b.left), height: viewport.height * (b.bottom - b.top) }
    }
    return { src, onZoom, getSize }
}

const makeTOCItem = item => ({
    label: item.title,
    href: JSON.stringify(item.dest),
    subitems: item.items.length ? item.items.map(makeTOCItem) : null,
})

export const makePDF = async file => {
    const rangeMode = typeof file === 'string' || !!file.url
    const source = rangeMode ? await openPDFSource(file, pdfjsLib) : null
    const transport = source?.transport ?? new pdfjsLib.PDFDataRangeTransport(file.size, [])
    if (!rangeMode) transport.requestDataRange = (begin, end) => {
        file.slice(begin, end).arrayBuffer().then(chunk => {
            transport.onDataRange(begin, chunk)
        })
    }
    const loading = pdfjsLib.getDocument({
        disableAutoFetch: true, disableStream: true, rangeChunkSize: CHUNK_SIZE,
        range: transport,
        cMapUrl: pdfjsPath('cmaps/'),
        standardFontDataUrl: pdfjsPath('standard_fonts/'),
        isEvalSupported: false,
    })
    let readyBook
    let rejectRange
    const rangeError = new Promise((_, reject) => { rejectRange = reject })
    source?.onError(error => { rejectRange(error); void loading.destroy().catch(() => {}); if (readyBook) readyBook.onPDFError?.(error) })
    const pdf = await Promise.race([loading.promise, rangeError])

    const book = { rendition: { layout: 'pre-paginated' }, rangeMode, rev: source?.rev }

    readyBook = book
    const { metadata, info } = await pdf.getMetadata() ?? {}
    // TODO: for better results, parse `metadata.getRaw()`
    book.metadata = {
        title: metadata?.get('dc:title') ?? info?.Title,
        author: metadata?.get('dc:creator') ?? info?.Author,
        contributor: metadata?.get('dc:contributor'),
        description: metadata?.get('dc:description') ?? info?.Subject,
        language: metadata?.get('dc:language'),
        publisher: metadata?.get('dc:publisher'),
        subject: metadata?.get('dc:subject'),
        identifier: metadata?.get('dc:identifier'),
        source: metadata?.get('dc:source'),
        rights: metadata?.get('dc:rights'),
    }

    const outline = await pdf.getOutline()
    book.toc = outline?.map(makeTOCItem)

    let options = {}, destroyed = false, common = null, cropTimer, cropLoading = false, cropReady = false
    const pageBoxes = new Map()
    const asBox = b => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite)
        ? { left: b[0], top: b[1], right: b[2], bottom: b[3] } : null
    const loadCrop = async () => {
        cropTimer = null
        if (destroyed || cropLoading || cropReady || !options.cropURL) return
        cropLoading = true
        try {
            const response = await fetch(options.cropURL, { credentials: 'same-origin' })
            if (!response.ok) return
            // Офлайн-копия может относиться к предыдущей редакции книги.
            const revision = response.headers.get('X-Book-SHA1')
            if (revision && options.rev && revision !== options.rev) return
            const result = await response.json()
            if (destroyed) return
            if (result.status === 'ready') {
                common = asBox(result.book)
                result.pages.forEach((b, i) => pageBoxes.set(i, asBox(b)))
                cropReady = true
                book.onPDFCropReady?.()
            }
        } catch { /* Офлайн читаем без обрезки до следующего обращения. */ }
        finally {
            cropLoading = false
            if (!destroyed && !cropReady && options.pdfCrop)
                cropTimer = setTimeout(loadCrop, 3000)
        }
    }
    let blend = 0, blendKey, imageMode
    const imageDocs = new Set()
    const getImageBlend = () => blend
    const setImageBlend = value => {
        blend = value
        if (blendKey) localStorage.setItem(blendKey, String(value))
        for (const ref of imageDocs) {
            const doc = ref.deref()
            if (!doc) { imageDocs.delete(ref); continue }
            const overlay = doc.querySelector('#image-original')
            if (overlay) overlay.style.opacity = value
        }
    }
    book.setPDFOptions = async value => {
        const key = value.imageBookKey ? `reader.pdf-images.${value.imageBookKey}` : null
        if (key !== blendKey || imageMode !== value.imageMode) {
            const stored = key ? localStorage.getItem(key) : null
            blend = imageMode !== undefined && imageMode !== value.imageMode ? imageModeBlend(value.imageMode)
                : stored !== null && Number.isFinite(Number(stored)) ? Math.max(0, Math.min(1, Number(stored))) : imageModeBlend(value.imageMode)
            blendKey = key; imageMode = value.imageMode
            setImageBlend(blend)
        }
        const path = rangeMode ? new URL(typeof file === 'string' ? file : file.url, location.href).pathname : ''
        options = { ...value, cropURL: value.cropURL ?? (file.cropURL || (/^\/api\/reader\/\d+\/file$/.test(path) ? path.replace(/file$/, 'pdf-crop') : null)) }
        if (value.cropData?.status === 'ready') {
            common = asBox(value.cropData.book)
            value.cropData.pages.forEach((b, i) => pageBoxes.set(i, asBox(b)))
            cropReady = true
        }
        if (options.pdfCrop && !cropTimer) void loadCrop()
    }
    const cache = new Map()
    const dimensions = new Map()
    const getBox = i => options.pdfCrop
        ? (pageBoxes.has(i) ? unionBoxes([common, pageBoxes.get(i)]) : (common ?? fullBox())) : fullBox()
    const sizeOf = i => {
        const base = dimensions.get(i), box = getBox(i)
        return { width: base.width * (box.right - box.left), height: base.height * (box.bottom - box.top) }
    }
    let prefetchGeneration = 0, prefetchRunning = false, activePages = []
    const prepared = new Set()
    const idle = globalThis.requestIdleCallback ?? (fn => setTimeout(fn, 100))
    book.prefetch = indices => {
        if (!rangeMode || destroyed) return
        if (indices.length === activePages.length && indices.every((i, n) => i === activePages[n])) return
        activePages = indices
        ++prefetchGeneration
        if (prefetchRunning) return
        prefetchRunning = true
        // Один исполнитель: при быстром перелистывании остаётся только один
        // уже начатый getOperatorList, следующая страница берётся из новой очереди.
        idle(async () => {
            try {
                let generation
                do {
                    generation = prefetchGeneration
                    const wanted = [...new Set(activePages.flatMap(i => prefetchPages(i, pdf.numPages)))]
                    for (const index of wanted) {
                        if (destroyed || generation !== prefetchGeneration) break
                        if (prepared.has(index)) continue
                        const page = await pdf.getPage(index + 1)
                        if (destroyed || generation !== prefetchGeneration) break
                        await page.getOperatorList()
                        prepared.add(index)
                        await new Promise(resolve => idle(resolve))
                    }
                } while (!destroyed && generation !== prefetchGeneration)
            } catch { /* Ошибку транспорта показывает основной путь чтения. */ }
            finally { prefetchRunning = false }
        })
    }
    book.sections = Array.from({ length: pdf.numPages }).map((_, i) => ({
        id: i,
        getSize: async () => {
            if (!dimensions.has(i)) dimensions.set(i, (await pdf.getPage(i + 1)).getViewport({ scale: 1 }))
            return sizeOf(i)
        },
        // После чтения метаданных размер доступен без асинхронной загрузки.
        getCachedSize: () => dimensions.has(i) ? sizeOf(i) : null,
        load: async () => {
            const cached = cache.get(i)
            if (cached) return cached
            dimensions.set(i, (await pdf.getPage(i + 1)).getViewport({ scale: 1 }))
            const url = await renderPage(await pdf.getPage(i + 1), false,
                () => getBox(i),
                () => ({ ...options, getImageBlend, setImageBlend }))
            const onZoom = url.onZoom
            const registered = new WeakSet()
            url.onZoom = async args => {
                if (!registered.has(args.doc)) {
                    registered.add(args.doc)
                    imageDocs.add(new WeakRef(args.doc))
                }
                await onZoom(args)
                prepared.add(i)
            }
            cache.set(i, url)
            return url
        },
        createDocument: async () => {
            // Та же структура textLayer, что в iframe, но без растрового рендера.
            // Поэтому CFI результата поиска разрешается в видимой странице.
            const section = await book.sections[i].load()
            const doc = new DOMParser().parseFromString(await fetchText(section.src), 'text/html')
            const page = await pdf.getPage(i + 1)
            await new pdfjsLib.TextLayer({
                textContentSource: await page.streamTextContent(),
                container: doc.querySelector('.textLayer'),
                viewport: page.getViewport({ scale: 1 }),
            }).render()
            return doc
        },
        size: 1000,
    }))
    book.isExternal = uri => /^\w+:/i.test(uri)
    book.resolveHref = async href => {
        const parsed = JSON.parse(href)
        const dest = typeof parsed === 'string'
            ? await pdf.getDestination(parsed) : parsed
        const index = typeof dest[0] === 'number' ? dest[0] : await pdf.getPageIndex(dest[0])
        return { index }
    }
    book.splitTOCHref = async href => {
        const parsed = JSON.parse(href)
        const dest = typeof parsed === 'string'
            ? await pdf.getDestination(parsed) : parsed
        const index = typeof dest[0] === 'number' ? dest[0] : await pdf.getPageIndex(dest[0])
        return [index, null]
    }
    book.getTOCFragment = doc => doc.documentElement
    book.getCover = async () => renderPage(await pdf.getPage(1), true)
    book.destroy = () => {
        destroyed = true
        clearTimeout(cropTimer)
        ++prefetchGeneration
        transport.abort?.()
        for (const section of cache.values()) URL.revokeObjectURL(section.src)
        cache.clear()
        return pdf.destroy()
    }
    return book
}
