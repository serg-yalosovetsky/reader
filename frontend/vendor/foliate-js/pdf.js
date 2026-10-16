import { contentBox, unionBoxes, stableBox, fullBox, pdfAppearance, darkTheme, imageBoxes } from './pdf-appearance.js'
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
    const appearance = pdfAppearance(options)
    doc.body.style.background = options.pdfBackground === 'theme' ? options.bg : 'white'
    Object.assign(doc.querySelector('#canvas').style, { filter: appearance.filter, mixBlendMode: appearance.blend })
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
    const canvasContext = canvas.getContext('2d')
    await page.render({ canvasContext, viewport }).promise
    if (options.pdfBackground === 'theme' && (darkTheme(options.theme) || ['sepia', 'gray', 'grey'].includes(options.theme))) {
        const parseColor = value => {
            const ctx = document.createElement('canvas').getContext('2d')
            ctx.fillStyle = value
            ctx.fillRect(0, 0, 1, 1)
            return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3)
        }
        const dark = darkTheme(options.theme)
        const colors = { dark, bg: parseColor(options.bg || '#ffffff'), fg: parseColor(options.fg || '#dddddd') }
        const pixels = canvasContext.getImageData(0, 0, canvas.width, canvas.height)
        const original = dark ? new Uint8ClampedArray(pixels.data) : null
        // Та же формула, что mapPixel, но без массивов на пиксель: на hi-DPI
        // странице миллионы пикселей, и аллокации растягивали рендер до секунд.
        const d = pixels.data, [br, bgc, bb] = colors.bg, [fr, fgc, fb] = colors.fg
        for (let i = 0; i < d.length; i += 4) {
            const r = d[i], g = d[i + 1], b = d[i + 2]
            if (!dark) { d[i] = r * br / 255; d[i + 1] = g * bgc / 255; d[i + 2] = b * bb / 255; continue }
            const hi = r > g ? (r > b ? r : b) : (g > b ? g : b)
            const lo = r < g ? (r < b ? r : b) : (g < b ? g : b)
            const keep = hi ? Math.min(1, (hi - lo) / hi * 2) : 0, light = (hi + lo) / 510, k = 1 - keep
            d[i] = keep * r + k * (fr * (1 - light) + br * light)
            d[i + 1] = keep * g + k * (fgc * (1 - light) + bgc * light)
            d[i + 2] = keep * b + k * (fb * (1 - light) + bb * light)
        }
        if (dark) {
            for (const b of imageBoxes(await page.getOperatorList(), pdfjsLib.OPS, viewport.transform))
                for (let y = Math.max(0, Math.floor(b.top)); y < Math.min(canvas.height, Math.ceil(b.bottom)); y++)
                    for (let x = Math.max(0, Math.floor(b.left)); x < Math.min(canvas.width, Math.ceil(b.right)); x++) {
                        const i = (y * canvas.width + x) * 4
                        for (let c = 0; c < 3; c++) pixels.data[i+c] = original[i+c] * (options.dimImages ? .85 : 1)
                    }
        }
        canvasContext.putImageData(pixels, 0, 0)
    }
    doc.querySelector('#canvas').replaceChildren(doc.adoptNode(canvas))

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
    const transport = new pdfjsLib.PDFDataRangeTransport(file.size, [])
    transport.requestDataRange = (begin, end) => {
        file.slice(begin, end).arrayBuffer().then(chunk => {
            transport.onDataRange(begin, chunk)
        })
    }
    const pdf = await pdfjsLib.getDocument({
        range: transport,
        cMapUrl: pdfjsPath('cmaps/'),
        standardFontDataUrl: pdfjsPath('standard_fonts/'),
        isEvalSupported: false,
    }).promise

    const book = { rendition: { layout: 'pre-paginated' } }

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

    let options = {}, sampled
    const bounds = new Map()
    const measure = i => {
        if (!bounds.has(i)) bounds.set(i, (async () => {
            const page = await pdf.getPage(i + 1)
            const base = page.getViewport({ scale: 1 })
            const viewport = page.getViewport({ scale: 320 / Math.max(base.width, base.height) })
            const canvas = document.createElement('canvas')
            canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height)
            const canvasContext = canvas.getContext('2d', { willReadFrequently: true })
            await page.render({ canvasContext, viewport }).promise
            return contentBox(canvasContext.getImageData(0, 0, canvas.width, canvas.height))
        })())
        return bounds.get(i)
    }
    let common = fullBox()
    book.setPDFOptions = async value => {
        options = value
        if (options.pdfCrop) {
            // Равномерная выборка по книге; объединение консервативнее медианы:
            // заголовки и колонтитулы остаются видны.
            sampled ??= (async () => {
                const indices = [...new Set(Array.from({ length: Math.min(8, pdf.numPages) }, (_, n) =>
                    Math.round(n * (pdf.numPages - 1) / Math.max(1, Math.min(8, pdf.numPages) - 1))))]
                const boxes = []
                for (const i of indices) boxes.push(await measure(i))
                return stableBox(boxes)
            })()
            common = await sampled
            await Promise.all([...cache.keys()].map(async i => { pageBoxes.set(i, await measure(i)) }))
        }
    }
    const pageBoxes = new Map()
    const cache = new Map()
    book.sections = Array.from({ length: pdf.numPages }).map((_, i) => ({
        id: i,
        load: async () => {
            const cached = cache.get(i)
            if (cached) return cached
            if (options.pdfCrop) pageBoxes.set(i, await measure(i))
            const url = await renderPage(await pdf.getPage(i + 1), false,
                () => options.pdfCrop ? (pageBoxes.get(i) ? unionBoxes([common, pageBoxes.get(i)]) : fullBox()) : fullBox(),
                () => options)
            cache.set(i, url)
            return url
        },
        size: 1000,
    }))
    book.isExternal = uri => /^\w+:/i.test(uri)
    book.resolveHref = async href => {
        const parsed = JSON.parse(href)
        const dest = typeof parsed === 'string'
            ? await pdf.getDestination(parsed) : parsed
        const index = await pdf.getPageIndex(dest[0])
        return { index }
    }
    book.splitTOCHref = async href => {
        const parsed = JSON.parse(href)
        const dest = typeof parsed === 'string'
            ? await pdf.getDestination(parsed) : parsed
        const index = await pdf.getPageIndex(dest[0])
        return [index, null]
    }
    book.getTOCFragment = doc => doc.documentElement
    book.getCover = async () => renderPage(await pdf.getPage(1), true)
    book.destroy = () => pdf.destroy()
    return book
}
