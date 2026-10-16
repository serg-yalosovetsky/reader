// Доля книги равномерна по страницам, независимо от их формата и обрезки.
export const toFraction = (index, offset, count) => (index + offset) / count
export const fromFraction = (fraction, count) => {
    const value = Math.max(0, Math.min(1, fraction)) * count
    const index = Math.min(count - 1, Math.floor(value))
    return { index, offset: value - index }
}
export const scrollPosition = (heights, top) => {
    let index = 0
    while (index < heights.length - 1 && top >= heights[index]) top -= heights[index++]
    return { index, offset: Math.max(0, Math.min(1, top / heights[index])) }
}
export const visiblePages = (heights, top, size, margin = 1) => {
    const first = scrollPosition(heights, top).index
    const last = scrollPosition(heights, top + Math.max(0, size - 1)).index
    return Array.from({ length: Math.min(heights.length - 1, last + margin)
        - Math.max(0, first - margin) + 1 }, (_, n) => Math.max(0, first - margin) + n)
}
