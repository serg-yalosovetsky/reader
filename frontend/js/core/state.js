// Общее мутабельное состояние читалки и библиотеки.
// Живые ES-биндинги: чтения импортируются как есть, реассайн — через setX().
export let view = null
export let currentWork = null
export let bookDoc = null
export let lastCfi = ''
export let lastIdx = null
export let _selIndex = -1
export let libWorks = [], libCalibre = [], libProgress = {}
export let libUpdated = new Set(), libMonitored = new Set()
export const navStack = []

export const setView = (v) => { view = v }
export const setCurrentWork = (w) => { currentWork = w }
export const setBookDoc = (d) => { bookDoc = d }
export const setLastCfi = (c) => { lastCfi = c }
export const setLastIdx = (i) => { lastIdx = i }
export const setSelIndex = (i) => { _selIndex = i }
export const setLibWorks = (v) => { libWorks = v }
export const setLibCalibre = (v) => { libCalibre = v }
export const setLibProgress = (v) => { libProgress = v }
export const setLibUpdated = (v) => { libUpdated = v }
export const setLibMonitored = (v) => { libMonitored = v }
