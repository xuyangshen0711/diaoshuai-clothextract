// These methods are used by the existing gallery and record views.
if (!Array.prototype.at) {
  Object.defineProperty(Array.prototype, 'at', {
    configurable: true, writable: true,
    value: function (this: unknown[], index: number) {
      const numeric = Number(index) || 0
      const integer = numeric < 0 ? Math.ceil(numeric) : Math.floor(numeric)
      const offset = integer < 0 ? this.length + integer : integer
      return offset < 0 || offset >= this.length ? undefined : this[offset]
    },
  })
}
if (!Object.hasOwn) {
  Object.defineProperty(Object, 'hasOwn', {
    configurable: true, writable: true,
    value: (object: object, key: PropertyKey) => Object.prototype.hasOwnProperty.call(object, key),
  })
}
