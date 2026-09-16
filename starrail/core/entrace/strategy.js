class StrategyApp {
  constructor(app, ctx, config) {
    ctx.guild().command('genshin.starrail.strategy', {authority: 1}).userFields(['id'])
      .shortcut(/^\*(.*)(攻略)$/)
      .action(async ({session}) => {
        new app.strategy(ctx, session).strategy()
      })
  }
}

module.exports = StrategyApp
