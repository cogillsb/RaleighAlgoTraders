# -*- coding: utf-8 -*-
"""
Created on Sat Oct  3 17:52:27 2020

@author: cogillsb
"""

import config
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta
from pytz import timezone
import talib as ta

class Account():
    def __init__(self):
        #Initialize connection
        self.api = tradeapi.REST(config.api_key_id, 
              config.api_secret,
              config.base_url,
              api_version='v2')
        
        self.portfolio_value = float(self.api.get_account().portfolio_value)
        self.open_orders = {}

class MarketState():
    '''Pulling current market data within 
    given constraints. Designed to work with
    trading algorithm'''
    
    def __init__(self, api, momentum=3.5,
                 max_share_price=13.0,
                 min_share_price=2.0,
                 min_last_dv=500000,
                 default_stop=.95):
        self.api = api
        self.momentum = momentum
        self.max_share_price = max_share_price
        self.min_share_price = min_share_price
        self.min_last_dv = min_last_dv
        self.default_stop = default_stop                
        self.market_open, self.market_close = self.set_market_time_period()
        self.tickers = self.get_tickers()
        self.symbols = [ticker.ticker for ticker in self.tickers]
        self.minute_history = self.get_1000m_history_data()
        self.ticker_dat = self.get_ticker_dat()
        
        
    def set_market_time_period(self):
        '''
        Get when the market opens or opened today
        '''
        nyc = timezone('America/New_York')
        today = datetime.today().astimezone(nyc)
        today_str = datetime.today().astimezone(nyc).strftime('%Y-%m-%d')
        calendar = self.api.get_calendar(start=today_str, end=today_str)[0]
        market_open = today.replace(
            hour=calendar.open.hour,
            minute=calendar.open.minute,
            second=0
        )
        market_open = market_open.astimezone(nyc)
        market_close = today.replace(
            hour=calendar.close.hour,
            minute=calendar.close.minute,
            second=0
        )
        market_close = market_close.astimezone(nyc)
        return market_open, market_close
        
    def get_tickers(self):
        '''Retrieve ticker data within constraints'''        
        tickers = self.api.polygon.all_tickers()
        assets = self.api.list_assets()
        symbols = [asset.symbol for asset in assets if asset.tradable]
        
        cur_tickers = []
        
        for t in tickers:
            if (t.ticker in symbols and
                t.lastTrade['p'] >= self.min_share_price and
                t.lastTrade['p'] <= self.max_share_price and
                t.prevDay['v'] * t.lastTrade['p'] > self.min_last_dv and
                t.todaysChangePerc >= self.momentum):
                cur_tickers.append(t)
        
        return cur_tickers
    
    def get_1000m_history_data(self):
        '''
        Get the history for each stock in dataframe
        '''
        minute_history = {}
        c = 0
        stop = datetime.today().strftime('%Y-%m-%d')
        start = (datetime.today()- timedelta(days=7)).strftime('%Y-%m-%d')
        for t in self.tickers:
            minute_history[t.ticker] = self.api.polygon.historic_agg_v2(
                t.ticker, 1, 'minute', _from=start, to=stop
            ).df.tail(1000)
            c += 1

        return minute_history
    
    def get_ticker_dat(self):
        '''
        Storing volume, previous close, and 15 min high in 
        accessible dict
        '''
        # Update initial state with information from tickers
        ticker_dat = {}
        for ticker in self.tickers:           
            ticker_dat[ticker.ticker] = {
                'p_close': ticker.prevDay['c'],
                'volume': ticker.day['v'], 
                '15m_high': self.minute_history[ticker.ticker][self.market_open:self.market_open+timedelta(minues=15)]['high'].max()
            }

        return ticker_dat 
            
    def update_table(self, data):
        '''
        Find the most recent minute count and update highs and lows
        if needed
        '''
        # First, aggregate 1s bars for up-to-date MACD calculations
        ts = data.start-timedelta(seconds=data.start.second, microseconds=data.start.microsecond)
        
        try:
            current = self.minute_history[data.symbol].loc[ts]
            self.minute_history[data.symbol].loc[ts] = [
                current.open,
                data.high if data.high > current.high else current.high,
                data.low if data.low < current.low else current.low,
                data.close,
                current.volume + data.volume
            ]
        except KeyError:
            self.minute_history[data.symbol].loc[ts] = [
                data.open,
                data.high,
                data.low,
                data.close,
                data.volume
            ]

class Controller():
    def __init__(self, account, market_state):
        #Initialize connection
        self.account = account
        self.market_state = market_state
        self.positions = self.get_positions()
        self.open_orders = {}
        self.partial_fills = {}
    
    def cancel_existing(self):    
        # Cancel any existing open orders on watched symbols
        existing_orders = self.account.api.list_orders(limit=500)
        for order in existing_orders:
            if order.symbol in self.market_state.symbols:
                self.api.cancel_order(order.id)
                
    def get_positions(self):
        '''
        Get current positions for stocks we are watching to be 
        updated during the run. 
        '''
        pos = {}        
        for p in [x for x in self.account.api.list_positions() if x in self.market_state.symbols ]:
            pos[p.symbol] = {
                        'qty' : float(p.qty),
                        'lcb' : float(p.cost_basis)
                    }

        return pos
            
    def handle_trade_update(self, data):
        '''
        Updating the open orders and position quantities
        '''
        if data.order['symbol'] in self.open_orders.keys():
            mv = -1 if data.order['side']=='sell' else 1
            if data.event in ['canceled', 'rejected']:
                self.open_orders[data.order['symbol']] = None
            else:                
                self.positions[data.order['symbol']]['qty'] += (mv * int(data.order['filled_qty']))
                self.open_orders[data.order['symbol']] = None if data.event=='fill' else data.order
            
            if self.positions[data.order['symbol']]['qty'] == 0: 
                del self.positions[data.order['symbol']]
            
    def cancel_old_orders(self, data, duration):
        '''
        Cancel existing orders and return bool on 
        continuing. 
        '''
        if data.order['symbol'] in self.open_orders.keys():
            order = self.open_orders[data.order['symbol']]
            if (data.start-order.submitted_at.astimezone(timezone('America/New_York')))//duration >1:
                self.account.api.cancel_order(order.id)
            return True
        return False
    
    
    def buy_order(self, data, shares):
        print('Submitting buy for {} shares of {} at {}'.format(shares, data.symbol, data.close))
        try:
            o = self.account.api.submit_order(
                symbol=data.symbol, qty=str(shares), side='buy',
                type='limit', time_in_force='day',
                limit_price=str(data.close)
            )
            self.positions[data.symbol] = {
                #qty will fill with order
                'qty' : 0,
                'lcb' : data.close
            }
            self.open_orders[data.symbol] = o           
        except Exception as e:
            print(e)
            
    def sell_order(self, data, typ):
        shares = self.positions[data.symbol]['qty']
        print('Submitting sell for {} shares of {} at {}'.format(shares, data.symbol, data.close))
        try:
            o = self.account.api.submit_order(
                symbol=data.symbol, qty=str(shares), side='sell',
                type=typ, time_in_force='day',
                limit_price=str(data.close)
            )
            self.open_orders[data.symbol] = o
        except Exception as e:
            print(e)
            
if __name__ == "__main__":
    acc = Account()
    ms = MarketState(acc.api)
    cnt = Controller(acc, ms)
    
    # Establish streaming connection
    conn = tradeapi.StreamConn(base_url=config.base_url, key_id=config.api_key_id, secret_key=config.api_secret)
    cnt.cancel_existing()    
    
    # Use trade updates to keep track of our portfolio
    @conn.on(r'trade_update')
    async def handle_trade_update(conn, channel, data):
        cnt.handle_trade_update(data)
    
    # Replace aggregated 1s bars with incoming 1m bars
    @conn.on(r'AM$')
    async def handle_minute_bar(conn, channel, data):
        ms.update_table(data)
    
    @conn.on(r'A$')
    async def handle_second_bar(conn, channel, data):
        '''
        Handling the up to the second data feed 
        from polygon stream for a specific
        equity
        '''
        #Update our table
        ms.update_table(data)
        #check for cancel signals
        if cnt.cancel_old_orders(data, 60):
            return
        
        #Strat starts here
        #Day trading part
        #Clear positions on closing of trading day
        if (ms.market_close - data.start) // 60 <= 15:
            #Clear out all of our positions
            if data.order['symbol'] in cnt.positions.keys():
                cnt.sell_order(data, 'market')
            conn.deregister(['A.{}'.format(data.symbol),'AM.{}'.format(data.symbol)])
    
        else:
            #Check for buy or sell conditions
            #Indicators
            #How much has the price changed since yesterday        
            daily_pct_change = (data.close - ms.ticker_dat[data.symbol]['p_close']) / ms.ticker_dat[data.symbol]['p_close']
            #Moving average convergence divergence
            cls = ms.minute_history[data.symbol]['close'].dropna()
            macd, macdsignal, macdhist = ta.MACD(cls, 12, 26, 9)
            macd_trnd = True if macdsignal[-1] > macd[-1] else False
            
            if data.symbol in cnt.positions.keys():    
                #Check for sale
                stop_price = cnt.positions[data.symbol]['lcb'] * ms.default_stop
                target_price =  data.close + ((data.close - stop_price) * 3)
                if(data.close <= stop_price or (data.close >= target_price and not macd_trnd)):
                    cnt.sell_order(data, 'limit')
            else:
                #check for buy
                if (daily_pct_change > .04 and
                    data.close > ms.ticker_dat[data.symbol]['15m_high'] and
                    ms.ticker_dat[data.symbol]['volume'] > 30000 and
                    macd_trnd):
                    #Buy
                    shares_to_buy = acc.portfolio_value * ms.risk // (data.close-(data.close * ms.default_stop))
                    cnt.buy_order(data, shares_to_buy)
        
    #Run in schedule for 15 minutes into trading
    channels = ['trade_updates']
    for symbol in ms.symbols:
        symbol_channels = ['A.{}'.format(symbol), 'AM.{}'.format(symbol)]
        channels += symbol_channels
        
    conn.run(channels)