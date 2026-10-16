import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
const source = readFileSync(new URL('../../frontend/vendor/foliate-js/pdf-appearance.js', import.meta.url), 'utf8')
const { contentBox, unionBoxes, stableBox, fullBox, mapPixel, imageBox, imageBoxes, multiplyMatrices, imageBlendDelta, imageModeBlend, themeImagePixel } = await import('data:text/javascript,' + encodeURIComponent(source))
const width = 100, height = 100, data = new Uint8ClampedArray(width * height * 4).fill(255)
assert.equal(contentBox({ data, width, height }), null)
for (let y = 20; y < 80; y++) for (let x = 30; x < 70; x++) data[(y * width + x) * 4] = 0
const box = contentBox({ data, width, height })
for (const [key, expected] of Object.entries({ left: .29, top: .19, right: .71, bottom: .81 })) assert.ok(Math.abs(box[key] - expected) < 1e-10)
// Единичная отметка на пустой странице не увеличивается до всего экрана.
const sparse = new Uint8ClampedArray(width * height * 4).fill(255)
sparse[200] = 0
assert.equal(contentBox({ data: sparse, width, height }), null)
assert.deepEqual(unionBoxes([null]), fullBox())
assert.deepEqual(stableBox([fullBox(), box, null]), box)
assert.deepEqual(unionBoxes([{ left: .1, top: .2, right: .8, bottom: .7 }, { left: .2, top: .1, right: .9, bottom: .8 }]), { left: .1, top: .1, right: .9, bottom: .8 })
for (const id of ['pdf-crop', 'dim-images']) {
  const html = readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8')
  assert.ok(html.includes(`id="${id}"`))
}
assert.ok(!readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8').includes('id="pdf-background"'))
console.log('PDF appearance OK')

const colors = {dark:true, bg:[43,46,51], fg:[221,225,231]}
assert.deepEqual(mapPixel([255,255,255], colors), colors.bg)
assert.deepEqual(mapPixel([0,0,0], colors), colors.fg)
assert.deepEqual(mapPixel([0,120,255], colors), [0,120,255])
assert.deepEqual(mapPixel([255,255,255], {...colors,dark:false}), colors.bg)
assert.deepEqual(mapPixel([0,0,0], {...colors,dark:false}), [0,0,0])
assert.deepEqual(imageBox([0,20,-30,0,50,10]), {left:20,top:10,right:50,bottom:30})
const OPS={save:1,restore:2,transform:3,paintImageXObject:4}
assert.deepEqual(imageBoxes({fnArray:[1,3,4,2,4],argsArray:[[],[20,0,0,30,10,15],[],[],[]]},OPS,[1,0,0,-1,0,100]),
 [{left:10,top:55,right:30,bottom:85},{left:0,top:99,right:1,bottom:100}])
assert.deepEqual(multiplyMatrices([1,0,0,1,0,0],[2,0,0,3,4,5]),[2,0,0,3,4,5])
const imageOPS = {save:1, restore:2, transform:3, paintImageXObject:4,
  paintInlineImageXObject:5, paintImageMaskXObject:6, paintSolidColorImageMask:7,
  paintImageXObjectRepeat:8, paintImageMaskXObjectRepeat:9,
  paintImageMaskXObjectGroup:10, paintInlineImageXObjectGroup:11,
  paintFormXObjectBegin:12, paintFormXObjectEnd:13}
const identity = [1,0,0,1,0,0]
for (const op of [5,6,7]) assert.deepEqual(imageBoxes({fnArray:[op],argsArray:[[]]},imageOPS,identity), [imageBox(identity)])
assert.deepEqual(imageBoxes({fnArray:[12,5,13,5],argsArray:[[[0,20,-30,0,50,10]],[],[],[]]},imageOPS,identity),
  [imageBox([0,20,-30,0,50,10]),imageBox(identity)])
for (const [op,args] of [[8,['img',20,30,[10,15,40,50]]],[9,[{},20,0,0,30,[10,15,40,50]]]])
  assert.deepEqual(imageBoxes({fnArray:[op],argsArray:[args]},imageOPS,identity),
    [imageBox([20,0,0,30,10,15]),imageBox([20,0,0,30,40,50])])
for (const [op,args] of [[10,[[{transform:[2,0,0,3,4,5]}]]],[11,[{},[{transform:[2,0,0,3,4,5]}]]]])
  assert.deepEqual(imageBoxes({fnArray:[op],argsArray:[args]},imageOPS,identity),[imageBox([2,0,0,3,4,5])])

// Старый выбор фона не возвращается в настройки даже при актуальной версии схемы.
let stored = JSON.stringify({ v: 3, pdfBackground: 'invert', theme: 'night' })
globalThis.localStorage = {
  getItem: () => stored,
  setItem: (_key, value) => { stored = value },
}
globalThis.document = { documentElement: { dataset: {} } }
const prefsSource = readFileSync(new URL('../../frontend/js/core/prefs.js', import.meta.url), 'utf8')
const { prefs, savePrefs } = await import('data:text/javascript,' + encodeURIComponent(prefsSource))
assert.equal('pdfBackground' in prefs, false)
assert.equal(prefs.theme, 'night')
assert.equal(prefs.menuAutohidePdf, true)
assert.equal(prefs.menuAutohideOther, false)
savePrefs()
assert.equal('pdfBackground' in JSON.parse(stored), false)

assert.equal(imageBlendDelta(.5, -120), .3)
assert.equal(imageBlendDelta(.5, 120), .7)
assert.equal(imageBlendDelta(.5, -9999), 0)
assert.equal(imageBlendDelta(.5, 9999), 1)
assert.equal(imageBlendDelta(0, 3, 1), .08)
assert.equal(imageBlendDelta(0, 1, 2, 300), .5)
assert.equal(imageBlendDelta(.4, 0), .4)
assert.equal(imageModeBlend('theme'), 0)
assert.equal(imageModeBlend('original'), 1)
assert.equal(imageModeBlend('dim'), .65)
assert.deepEqual(themeImagePixel([255,255,255], colors.bg, colors.fg), colors.bg)
assert.deepEqual(themeImagePixel([0,0,0], colors.bg, colors.fg), colors.fg)
for (const rgb of [[240,60,20], [128,128,128], [0,120,255]]) {
  const mapped = themeImagePixel(rgb, colors.bg, colors.fg)
  mapped.forEach((v,i) => assert.ok(v >= colors.bg[i] && v <= colors.fg[i]))
}
