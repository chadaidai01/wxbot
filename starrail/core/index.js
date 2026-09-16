const init = require('../index.js')
const StarRailApp = require("./entrace/starrail");
const StrategyApp = require("./entrace/strategy");
class StarRailPlugin {

  constructor(ctx, config) {
    // ready
    ctx.on("ready", async ()=>{
      this.apps = await init()
      new StarRailApp(this.apps, ctx, config)
      new StrategyApp(this.apps, ctx, config)
    })
  }
}

exports.default = StarRailPlugin
exports.name = 'starrail-plugin'
