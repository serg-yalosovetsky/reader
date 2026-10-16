// Нормализованные границы: единый масштаб независимо от DPI и размера страницы.
export const fullBox = () => ({ left: 0, top: 0, right: 1, bottom: 1 })
export function contentBox({ data, width, height }) {
    let left = width, top = height, right = 0, bottom = 0, count = 0
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
        const i = (y * width + x) * 4
        if (data[i + 3] > 32 && Math.min(data[i], data[i + 1], data[i + 2]) < 235) {
            left = Math.min(left, x); right = Math.max(right, x + 1)
            top = Math.min(top, y); bottom = Math.max(bottom, y + 1); count++
        }
    }
    // Номер страницы или несколько точек не должны становиться целой страницей.
    if (count < width * height * .002 || right - left < width * .1 || bottom - top < height * .1) return null
    const padding = (right - left) * .025
    return { left: Math.max(0, (left - padding) / width), top: Math.max(0, (top - padding) / height),
        right: Math.min(1, (right + padding) / width), bottom: Math.min(1, (bottom + padding) / height) }
}
export function unionBoxes(boxes) {
    const valid = boxes.filter(Boolean)
    return valid.length ? { left: Math.min(...valid.map(b => b.left)), top: Math.min(...valid.map(b => b.top)),
        right: Math.max(...valid.map(b => b.right)), bottom: Math.max(...valid.map(b => b.bottom)) } : fullBox()
}
// Полноформатная обложка не отменяет обрезку полей у остальных страниц.
// На самой обложке её собственный bbox всё равно расширит окно до полного.
export function stableBox(boxes) {
    return unionBoxes(boxes.filter(b => b && (b.right - b.left) * (b.bottom - b.top) < .9))
}
export const darkTheme = theme => ['dusk', 'night', 'terminal', 'black', 'phosphor'].includes(theme)
export function pdfAppearance({ theme, pdfBackground = 'original', dimImages = true }) {
    return { filter: pdfBackground === 'invert' ? 'invert(1) hue-rotate(180deg)' : pdfBackground === 'original' && darkTheme(theme) && dimImages ? 'brightness(.85)' : 'none', blend: 'normal' }
}
export const multiplyMatrices = (a,b) => [a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1], a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3], a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5]]
export function imageBox(m) {
    const points = [[0,0],[1,0],[0,1],[1,1]].map(([x,y]) => [m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5]])
    return {left: Math.min(...points.map(p=>p[0])), top: Math.min(...points.map(p=>p[1])), right: Math.max(...points.map(p=>p[0])), bottom: Math.max(...points.map(p=>p[1]))}
}
export function imageBoxes(list, OPS, viewport) {
    let matrix = viewport
    const stack = [], boxes = []
    const add = (m = matrix) => boxes.push(imageBox(m))
    for (let i=0; i<list.fnArray.length; i++) {
        const op=list.fnArray[i], args=list.argsArray[i]
        if (op === OPS.save) stack.push(matrix)
        else if (op === OPS.restore) matrix=stack.pop() ?? viewport
        else if (op === OPS.transform) matrix=multiplyMatrices(matrix,args)
        else if (op === OPS.paintFormXObjectBegin) { stack.push(matrix); if(args[0]) matrix=multiplyMatrices(matrix,args[0]) }
        else if (op === OPS.paintFormXObjectEnd) matrix=stack.pop() ?? viewport
        else if ([OPS.paintImageXObject,OPS.paintInlineImageXObject,OPS.paintImageMaskXObject,OPS.paintSolidColorImageMask].includes(op)) add()
        else if (op === OPS.paintImageXObjectRepeat || op === OPS.paintImageMaskXObjectRepeat) {
            const [a,b,c,d,positions] = op === OPS.paintImageMaskXObjectRepeat ? args.slice(1) : [args[1],0,0,args[2],args[3]]
            for(let j=0;j<positions.length;j+=2) add(multiplyMatrices(matrix,[a,b,c,d,positions[j],positions[j+1]]))
        } else if (op === OPS.paintImageMaskXObjectGroup || op === OPS.paintInlineImageXObjectGroup)
            for(const entry of (op === OPS.paintImageMaskXObjectGroup ? args[0] : args[1])) add(multiplyMatrices(matrix,entry.transform))
    }
    return boxes
}
export function mapPixel(rgb, {dark,bg,fg}) {
    if (!dark) return rgb.map((v,i)=>Math.round(v*bg[i]/255))
    const hi=Math.max(...rgb), lo=Math.min(...rgb), keep=Math.min(1,hi ? (hi-lo)/hi*2 : 0), light=(hi+lo)/510
    return rgb.map((v,i)=>Math.round(keep*v+(1-keep)*(fg[i]*(1-light)+bg[i]*light)))
}
