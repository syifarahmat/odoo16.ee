# -*- coding: utf-8 -*-

import datetime
from lxml import etree
from dateutil.relativedelta import relativedelta
import re
import logging
from pytz import timezone
from urllib.parse import urlencode, quote

import requests
import requests.adapters
from urllib3.util.ssl_ import create_urllib3_context

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT

class LegacyHTTPAdapter(requests.adapters.HTTPAdapter):
    """ An adapter to allow unsafe legacy renegotiation necessary to connect to
    gravely outdated ETA production servers.
    """

    def init_poolmanager(self, *args, **kwargs):
        # This is not defined before Python 3.12
        # cfr. https://github.com/python/cpython/pull/93927
        # Origin: https://github.com/openssl/openssl/commit/ef51b4b9
        OP_LEGACY_SERVER_CONNECT = 0x04
        context = create_urllib3_context(options=OP_LEGACY_SERVER_CONNECT)
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)

BANXICO_DATE_FORMAT = '%d/%m/%Y'
CBUAE_URL = "https://centralbank.ae/umbraco/Surface/Exchange/GetExchangeRateAllCurrency"
CBEGY_URL = "https://www.cbe.org.eg/en/economic-research/statistics/cbe-exchange-rates"
MAP_CURRENCIES = {
    'US Dollar': 'USD',
    'UAE Dirham': 'AED',
    'Argentine Peso': 'ARS',
    'Australian Dollar': 'AUD',
    'Azerbaijan manat': 'AZN',
    'Bangladesh Taka': 'BDT',
    'Bulgarian lev': 'BGN',
    'Bahrani Dinar': 'BHD',
    'Bahraini Dinar': 'BHD',
    'Brunei Dollar': 'BND',
    'Brazilian Real': 'BRL',
    'Botswana Pula': 'BWP',
    'Belarus Rouble': 'BYN',
    'Canadian Dollar': 'CAD',
    'Swiss Franc': 'CHF',
    'Chilean Peso': 'CLP',
    'Chinese Yuan - Offshore': 'CNY',
    'Chinese Yuan': 'CNY',
    'Colombian Peso': 'COP',
    'Czech Koruna': 'CZK',
    'Danish Krone': 'DKK',
    'Algerian Dinar': 'DZD',
    'Egypt Pound': 'EGP',
    'Ethiopian birr': 'ETB',
    'Euro': 'EUR',
    'GB Pound': 'GBP',
    'Pound Sterling': 'GBP',
    'Hongkong Dollar': 'HKD',
    'Croatian kuna': 'HRK',
    'Hungarian Forint': 'HUF',
    'Indonesia Rupiah': 'IDR',
    'Israeli new shekel': 'ILS',
    'Indian Rupee': 'INR',
    'Iraqi dinar': 'IQD',
    'Iceland Krona': 'ISK',
    'Jordan Dinar': 'JOD',
    'Jordanian Dinar': 'JOD',
    'Japanese Yen': 'JPY',
    'Japanese Yen 100': 'JPY',
    'Kenya Shilling': 'KES',
    'Korean Won': 'KRW',
    'Kuwaiti Dinar': 'KWD',
    'Kazakhstan Tenge': 'KZT',
    'Lebanon Pound': 'LBP',
    'Sri Lanka Rupee': 'LKR',
    'Libyan dinar': 'LYD',
    'Moroccan Dirham': 'MAD',
    'Macedonia Denar': 'MKD',
    'Mauritian rupee': 'MUR',
    'Mexican Peso': 'MXN',
    'Malaysia Ringgit': 'MYR',
    'Nigerian Naira': 'NGN',
    'Norwegian Krone': 'NOK',
    'NewZealand Dollar': 'NZD',
    'Omani Rial': 'OMR',
    'Omani Riyal': 'OMR',
    'Peru Sol': 'PEN',
    'Philippine Piso': 'PHP',
    'Pakistan Rupee': 'PKR',
    'Polish Zloty': 'PLN',
    'Qatari Riyal': 'QAR',
    'Romanian leu': 'RON',
    'Serbian Dinar': 'RSD',
    'Russia Rouble': 'RUB',
    'Saudi Riyal': 'SAR',
    'Singapore Dollar': 'SGD',
    'Swedish Krona': 'SWK',
    'Syrian pound': 'SYP',
    'Thai Baht': 'THB',
    'Turkmen manat': 'TMT',
    'Tunisian Dinar': 'TND',
    'Turkish Lira': 'TRY',
    'Trin Tob Dollar': 'TTD',
    'Taiwan Dollar': 'TWD',
    'Tanzania Shilling': 'TZS',
    'Uganda Shilling': 'UGX',
    'Uzbekistani som': 'UZS',
    'Vietnam Dong': 'VND',
    'Yemen Rial': 'YER',
    'South Africa Rand': 'ZAR',
    'Zambian Kwacha': 'ZMW',
}

_logger = logging.getLogger(__name__)


def xml2json_from_elementtree(el, preserve_whitespaces=False):
    """ xml2json-direct
    Simple and straightforward XML-to-JSON converter in Python
    New BSD Licensed
    http://code.google.com/p/xml2json-direct/
    """
    res = {}
    if el.tag[0] == "{":
        ns, name = el.tag.rsplit("}", 1)
        res["tag"] = name
        res["namespace"] = ns[1:]
    else:
        res["tag"] = el.tag
    res["attrs"] = {}
    for k, v in el.items():
        res["attrs"][k] = v
    kids = []
    if el.text and (preserve_whitespaces or el.text.strip() != ''):
        kids.append(el.text)
    for kid in el:
        kids.append(xml2json_from_elementtree(kid, preserve_whitespaces))
        if kid.tail and (preserve_whitespaces or kid.tail.strip() != ''):
            kids.append(kid.tail)
    res["children"] = kids
    return res


# countries, provider_code, description
CURRENCY_PROVIDER_SELECTION = [
    ([], 'ecb', 'European Central Bank'),
    ([], 'xe_com', 'xe.com'),
    (['AE'], 'cbuae', 'UAE Central Bank'),
    (['CA'], 'boc', 'Bank Of Canada'),
    (['CH'], 'fta', 'Federal Tax Administration (Switzerland)'),
    (['CL'], 'mindicador', 'Chilean mindicador.cl'),
    (['EG'], 'cbegy', 'Central Bank of Egypt'),
    (['MX'], 'banxico', 'Mexican Bank'),
    (['PE'], 'bcrp', 'SUNAT (replaces Bank of Peru)'),
    (['RO'], 'bnr', 'National Bank Of Romania'),
    (['TR'], 'tcmb', 'Turkey Republic Central Bank'),
    (['PL'], 'nbp', 'National Bank of Poland'),
    (['BR'], 'bbr', 'Central Bank of Brazil'),
]

class ResCompany(models.Model):
    _inherit = 'res.company'

    currency_interval_unit = fields.Selection(
        selection=[
            ('manually', 'Manually'),
            ('daily', 'Daily'),
            ('weekly', 'Weekly'),
            ('monthly', 'Monthly')
        ],
        default='manually',
        required=True,
        string='Interval Unit',
    )
    currency_next_execution_date = fields.Date(string="Next Execution Date")
    currency_provider = fields.Selection(
        selection=[(provider_code, desc) for dummy, provider_code, desc in CURRENCY_PROVIDER_SELECTION],
        string='Service Provider',
        compute='_compute_currency_provider',
        readonly=False,
        store=True,
    )

    @api.depends('country_id')
    def _compute_currency_provider(self):
        code_providers = {
            country: provider_code
            for countries, provider_code, dummy in CURRENCY_PROVIDER_SELECTION
            for country in countries
        }
        for record in self:
            record.currency_provider = code_providers.get(record.country_id.code, 'ecb')

    def update_currency_rates(self):
        ''' This method is used to update all currencies given by the provider.
        It calls the parse_function of the selected exchange rates provider automatically.

        For this, all those functions must be called _parse_xxx_data, where xxx
        is the technical name of the provider in the selection field. Each of them
        must also be such as:
            - It takes as its only parameter the recordset of the currencies
              we want to get the rates of
            - It returns a dictionary containing currency codes as keys, and
              the corresponding exchange rates as its values. These rates must all
              be based on the same currency, whatever it is. This dictionary must
              also include a rate for the base currencies of the companies we are
              updating rates from, otherwise this will result in an error
              asking the user to choose another provider.

        :return: True if the rates of all the records in self were updated
                 successfully, False if at least one wasn't.
        '''
        active_currencies = self.env['res.currency'].search([])
        rslt = True
        for (currency_provider, companies) in self._group_by_provider().items():
            parse_function = getattr(companies, '_parse_' + currency_provider + '_data')
            try:
                parse_results = parse_function(active_currencies)
                companies._generate_currency_rates(parse_results)
            except Exception:
                rslt = False
                _logger.exception('Unable to connect to the online exchange rate platform %s. The web service may be temporary down.', currency_provider)
        return rslt

    def _group_by_provider(self):
        """ Returns a dictionnary grouping the companies in self by currency
        rate provider. Companies with no provider defined will be ignored."""
        rslt = {}
        for company in self:
            if not company.currency_provider:
                continue

            if rslt.get(company.currency_provider):
                rslt[company.currency_provider] += company
            else:
                rslt[company.currency_provider] = company
        return rslt

    def _generate_currency_rates(self, parsed_data):
        """ Generate the currency rate entries for each of the companies, using the
        result of a parsing function, given as parameter, to get the rates data.

        This function ensures the currency rates of each company are computed,
        based on parsed_data, so that the currency of this company receives rate=1.
        This is done so because a lot of users find it convenient to have the
        exchange rate of their main currency equal to one in Odoo.
        """
        Currency = self.env['res.currency']
        CurrencyRate = self.env['res.currency.rate']

        for company in self:
            rate_info = parsed_data.get(company.currency_id.name, None)

            if not rate_info:
                msg = _("Your main currency (%s) is not supported by this exchange rate provider. Please choose another one.", company.currency_id.name)
                if self._context.get('suppress_errors'):
                    _logger.warning(msg)
                    continue
                else:
                    raise UserError(msg)

            base_currency_rate = rate_info[0]

            for currency, (rate, date_rate) in parsed_data.items():
                rate_value = rate / base_currency_rate

                currency_object = Currency.search([('name', '=', currency)])
                if currency_object:  # if rate provider base currency is not active, it will be present in parsed_data
                    already_existing_rate = CurrencyRate.search([('currency_id', '=', currency_object.id), ('name', '=', date_rate), ('company_id', '=', company.id)])
                    if already_existing_rate:
                        already_existing_rate.rate = rate_value
                    else:
                        CurrencyRate.create({'currency_id': currency_object.id, 'rate': rate_value, 'name': date_rate, 'company_id': company.id})

    def _parse_fta_data(self, available_currencies):
        ''' Parses the data returned in xml by FTA servers and returns it in a more
        Python-usable form.'''
        request_url = 'https://www.backend-rates.bazg.admin.ch/api/xmldaily?d=yesterday&locale=en'
        response = requests.get(request_url, timeout=30)
        response.raise_for_status()

        rates_dict = {}
        available_currency_names = available_currencies.mapped('name')
        xml_tree = etree.fromstring(response.content)
        data = xml2json_from_elementtree(xml_tree)
        # valid dates (gueltigkeit) may be comma separated, the first one will do
        date_elem = xml_tree.xpath("//*[local-name() = 'gueltigkeit']")[0]
        date_rate = datetime.datetime.strptime(date_elem.text.split(',')[0], '%d.%m.%Y').date()
        for child_node in data['children']:
            if child_node['tag'] == 'devise':
                currency_code = child_node['attrs']['code'].upper()

                if currency_code in available_currency_names:
                    currency_xml = None
                    rate_xml = None

                    for sub_child in child_node['children']:
                        if sub_child['tag'] == 'waehrung':
                            currency_xml = sub_child['children'][0]
                        elif sub_child['tag'] == 'kurs':
                            rate_xml = sub_child['children'][0]
                        if currency_xml and rate_xml:
                            #avoid iterating for nothing on children
                            break

                    rates_dict[currency_code] = (float(re.search(r'\d+', currency_xml).group()) / float(rate_xml), date_rate)

        if 'CHF' in available_currency_names:
            rates_dict['CHF'] = (1.0, date_rate)

        return rates_dict

    def _parse_ecb_data(self, available_currencies):
        ''' This method is used to update the currencies by using ECB service provider.
            Rates are given against EURO
        '''
        request_url = "http://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
        response = requests.get(request_url, timeout=30)
        response.raise_for_status()

        xmlstr = etree.fromstring(response.content)
        data = xml2json_from_elementtree(xmlstr)
        node = data['children'][2]['children'][0]
        xmldate = fields.Date.to_date(node['attrs']['time'])
        available_currency_names = available_currencies.mapped('name')
        rslt = {x['attrs']['currency']:(float(x['attrs']['rate']), xmldate) for x in node['children'] if x['attrs']['currency'] in available_currency_names}

        if rslt and 'EUR' in available_currency_names:
            rslt['EUR'] = (1.0, xmldate)

        return rslt

    def _parse_cbuae_data(self, available_currencies):
        ''' This method is used to update the currencies by using UAE Central Bank service provider.
            Exchange rates are expressed as 1 unit of the foreign currency converted into AED
        '''
        headers = {
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.centralbank.ae/en/forex-eibor/exchange-rates/'
        }

        response = requests.get(CBUAE_URL, headers=headers, timeout=30)
        response.raise_for_status()

        htmlelem = etree.fromstring(response.content, etree.HTMLParser(encoding='utf-8'))
        rates_entries = htmlelem.xpath("//table/tbody//tr")
        date_elem = htmlelem.xpath("//div[@class='row mb-4']/div/p[last()]")[0]
        date_rate = datetime.datetime.strptime(
            date_elem.text.strip(),
            'Last updated:\r\n\r\n%A %d %B %Y %I:%M:%S %p').date()
        available_currency_names = set(available_currencies.mapped('name'))
        rslt = {}
        for rate_entry in rates_entries:
            # line structure is <td>Currency Description</td><td>rate</td>
            currency_code = MAP_CURRENCIES.get(rate_entry[1].text)
            rate = float(rate_entry[2].text)
            if currency_code in available_currency_names:
                rslt[currency_code] = (1.0/rate, date_rate)

        if 'AED' in available_currency_names:
            rslt['AED'] = (1.0, date_rate)
        return rslt

    def _parse_cbegy_data(self, available_currencies):
        ''' This method is used to update the currencies by using the Central Bank of Egypt service provider.
            Exchange rates are expressed as 1 unit of the foreign currency converted into EGP
        '''
        headers = {
            'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.41 Safari/537.36',
        }
        fetched_data = requests.get(CBEGY_URL, headers=headers, timeout=30)
        fetched_data.raise_for_status()

        htmlelem = etree.fromstring(fetched_data.content, etree.HTMLParser())
        rates_entries = htmlelem.xpath("//table/tbody/tr")
        date_text = htmlelem.xpath("//p[contains(.,'Rates for Date')]/text()")[1]
        date_rate = datetime.datetime.strptime(date_text.strip(), 'Rates for Date: %d/%m/%Y').date()
        available_currency_names = set(available_currencies.mapped('name'))
        rslt = {}
        for rate_entry in rates_entries:
            currency_code = MAP_CURRENCIES.get(rate_entry[0].text.strip())
            # line structure is <td>Currency Description</td><td>BUY RATE</td><td>SELL RATE</td>
            # we use the average of SELL and BUY rates
            rate = (float(rate_entry[1].text) + float(rate_entry[2].text)) / 2
            if currency_code in available_currency_names:
                rslt[currency_code] = (1.0/rate, date_rate)

        if 'EGP' in available_currency_names:
            rslt['EGP'] = (1.0, date_rate)
        return rslt

    def _parse_bbr_data(self, available_currencies):
        ''' This method is used to update the currencies by using the Central Bank of Brazil service provider.
            Exchange rates are expressed as 1 unit of the foreign currency converted into BRL.
        '''
        def _get_currency_exchange_rate(session, cur, date):
            '''Returns the rate for the given day and currency if found, None if there were no currency changes that day.
            '''
            query_params = {
                "@moeda": ("'%s'" % cur),
                "@dataCotacao": ("'%s'" % date),
                "$top": "1",
                "$orderby": "dataHoraCotacao desc",
                "$format": "json",
                "$select": "cotacaoCompra",
            }
            encoded_params = urlencode(query_params, safe="@$'", quote_via=quote)
            request_url = "https://olinda.bcb.gov.br/olinda/service/PTAX/version/v1/odata/ExchangeRateDate(moeda=@moeda,dataCotacao=@dataCotacao)"
            response = session.get(request_url, params=encoded_params, timeout=10)
            response.raise_for_status()
            if 'application/json' not in response.headers.get('Content-Type', ''):
                raise ValueError('Should be json')
            data = response.json()

            # If there were no currency changes that day, return None.
            if not data['value']:
                return None
            bid_rate = data['value'][0]['cotacaoCompra']
            return bid_rate

        # Using a session since we're doing multiple requests.
        session = requests.Session()
        # Get the currencies from the bank.
        request_url = "https://olinda.bcb.gov.br/olinda/service/PTAX/version/v1/odata/Currencies?$top=100&$format=json"
        response = session.get(request_url, timeout=10)
        response.raise_for_status()
        if 'application/json' not in response.headers.get('Content-Type', ''):
            raise ValueError('Should be json')
        data = response.json()
        available_currency_names = available_currencies.mapped('name')
        currencies = [val['simbolo'] for val in data['value'] if val['simbolo'] in available_currency_names]

        date_rate = datetime.datetime.now(timezone('America/Sao_Paulo'))

        # For every available currency in the returned currencies, if it's in the
        # available currencies, get its exchange rate.
        rslt = {}
        for currency in currencies:
            # As there are days where there are no currency changes, we start by calling
            # the api with the current day, and keep decrementing the date by one day until
            # we reach a day with currency changes.
            rate = None
            while not rate:
                rate = _get_currency_exchange_rate(session, currency, date_rate.strftime("%m-%d-%Y"))
                if not rate:
                    date_rate = date_rate - datetime.timedelta(days=1)

            rslt[currency] = (1.0/rate, date_rate)

        if 'BRL' in available_currency_names:
            rslt['BRL'] = (1.0, date_rate)

        return rslt

    def _parse_boc_data(self, available_currencies):
        """This method is used to update currencies exchange rate by using Bank
           Of Canada daily exchange rate service.
           Exchange rates are expressed as 1 unit of the foreign currency converted into Canadian dollars.
           Keys are in this format: 'FX{CODE}CAD' e.g.: 'FXEURCAD'
        """
        available_currency_names = available_currencies.mapped('name')

        request_url = "http://www.bankofcanada.ca/valet/observations/group/FX_RATES_DAILY/json"
        response = requests.get(request_url, timeout=30)
        response.raise_for_status()
        if not 'application/json' in response.headers.get('Content-Type', ''):
            raise ValueError('Should be json')
        data = response.json()

        # 'observations' key contains rates observations by date
        last_observation_date = sorted([obs['d'] for obs in data['observations']])[-1]
        last_obs = [obs for obs in data['observations'] if obs['d'] == last_observation_date][0]
        last_obs.update({'FXCADCAD': {'v': '1'}})
        date_rate = datetime.datetime.strptime(last_observation_date, "%Y-%m-%d").date()
        rslt = {}
        if 'CAD' in available_currency_names:
            rslt['CAD'] = (1, date_rate)

        for currency_name in available_currency_names:
            currency_obs = last_obs.get('FX{}CAD'.format(currency_name), None)
            if currency_obs is not None:
                rslt[currency_name] = (1.0/float(currency_obs['v']), date_rate)

        return rslt

    def _parse_banxico_data(self, available_currencies):
        """Parse function for Banxico provider.
        * With basement in legal topics in Mexico the rate must be **one** per day and it is equal to the rate known the
        day immediate before the rate is gotten, it means the rate for 02/Feb is the one at 31/jan.
        * The base currency is always MXN but with the inverse 1/rate.
        * The official institution is Banxico.
        * The webservice returns the following currency rates:
            - SF46410 EUR
            - SF60632 CAD
            - SF43718 USD Fixed
            - SF46407 GBP
            - SF46406 JPY
            - SF60653 USD SAT - Officially used from SAT institution
        Source: http://www.banxico.org.mx/portal-mercado-cambiario/
        """
        icp = self.env['ir.config_parameter'].sudo()
        token = icp.get_param('banxico_token')
        if not token:
            # https://www.banxico.org.mx/SieAPIRest/service/v1/token
            token = 'f9456e38aa6d8336c17f9a6c71fabddb1ee5101c6e83d4c1babccd70198aad38'  # noqa
            icp.set_param('banxico_token', token)
        foreigns = {
            # position order of the rates from webservices
            'SF46410': 'EUR',
            'SF60632': 'CAD',
            'SF46406': 'JPY',
            'SF46407': 'GBP',
            'SF60653': 'USD',
        }
        url = 'https://www.banxico.org.mx/SieAPIRest/service/v1/series/%s/datos/%s/%s?token=%s' # noqa
        date_mx = datetime.datetime.now(timezone('America/Mexico_City'))
        today = date_mx.strftime(DEFAULT_SERVER_DATE_FORMAT)
        yesterday = (date_mx - datetime.timedelta(days=1)).strftime(DEFAULT_SERVER_DATE_FORMAT)
        res = requests.get(url % (','.join(foreigns), yesterday, today, token), timeout=30)
        res.raise_for_status()
        series = res.json()['bmx']['series']
        series = {serie['idSerie']: {dato['fecha']: dato['dato'] for dato in serie['datos']} for serie in series if 'datos' in serie}

        available_currency_names = available_currencies.mapped('name')

        rslt = {
            'MXN': (1.0, fields.Date.today().strftime(DEFAULT_SERVER_DATE_FORMAT)),
        }

        today = date_mx.strftime(BANXICO_DATE_FORMAT)
        yesterday = (date_mx - datetime.timedelta(days=1)).strftime(BANXICO_DATE_FORMAT)
        for index, currency in foreigns.items():
            if not series.get(index, False):
                continue
            if currency not in available_currency_names:
                continue

            serie = series[index]
            for rate in serie:
                try:
                    foreign_mxn_rate = float(serie[rate])
                except (ValueError, TypeError):
                    continue
                foreign_rate_date = datetime.datetime.strptime(rate, BANXICO_DATE_FORMAT).strftime(DEFAULT_SERVER_DATE_FORMAT)
                rslt[currency] = (1.0/foreign_mxn_rate, foreign_rate_date)
        return rslt

    def _parse_xe_com_data(self, available_currencies):
        """ Parses the currency rates data from xe.com provider.
        As this provider does not have an API, we directly extract what we need
        from HTML.
        """
        url_format = 'http://www.xe.com/currencytables/?from=%(currency_code)s'

        # We generate all the exchange rates relative to the USD. This is purely arbitrary.
        response = requests.get(url_format % {'currency_code': 'USD'}, timeout=30)
        response.raise_for_status()

        rslt = {}

        available_currency_names = available_currencies.mapped('name')

        htmlelem = etree.fromstring(response.content, etree.HTMLParser())
        rates_entries = htmlelem.xpath(".//div[@id='table-section']//tbody/tr")
        time_element = htmlelem.xpath(".//div[@id='table-section']/section/p")[0]
        date_rate = datetime.datetime.strptime(time_element.text, '%b %d, %Y, %H:%M UTC').date()

        if 'USD' in available_currency_names:
            rslt['USD'] = (1.0, date_rate)

        for rate_entry in rates_entries:
            # line structure is <th>CODE</th><td>NAME<td><td>UNITS PER CURRENCY</td><td>CURRENCY PER UNIT</td>
            currency_code = ''.join(rate_entry.find('.//th').itertext()).strip()
            if currency_code in available_currency_names:
                rate = float(rate_entry.find("td[2]").text.replace(',', ''))
                rslt[currency_code] = (rate, date_rate)

        return rslt

    def _parse_bnr_data(self, available_currencies):
        ''' This method is used to update the currencies by using
        BNR service provider. Rates are given against RON
        '''
        request_url = "https://www.bnr.ro/nbrfxrates.xml"
        response = requests.get(request_url, timeout=30)
        response.raise_for_status()

        xmlstr = etree.fromstring(response.content)
        data = xml2json_from_elementtree(xmlstr)
        available_currency_names = available_currencies.mapped('name')
        rate_date = fields.Date.today()
        rslt = {}
        rates_node = data['children'][1]['children'][2]
        if rates_node:
            # Rates are valid for the next day, refer:
            # https://lege5.ro/Gratuit/ha4tomrvge/cursul-de-schimb-valutar-norma-metodologica?dp=ha3tgmzwgu2dk
            rate_date = (datetime.datetime.strptime(
                rates_node['attrs']['date'], DEFAULT_SERVER_DATE_FORMAT
            ) + datetime.timedelta(days=1)).strftime(DEFAULT_SERVER_DATE_FORMAT)
            for x in rates_node['children']:
                if x['attrs']['currency'] in available_currency_names:
                    rslt[x['attrs']['currency']] = (
                        float(x['attrs'].get('multiplier', '1')) / float(x['children'][0]),
                        rate_date
                    )
        if rslt and 'RON' in available_currency_names:
            rslt['RON'] = (1.0, rate_date)
        return rslt

    def _parse_bcrp_data(self, available_currencies):
        """Sunat
        Source: https://www.sunat.gob.pe/descarga/TipoCambio.txt
        * The value of the rate is the "official" rate
        * The base currency is always PEN but with the inverse 1/rate.
        """

        result = {}
        available_currency_names = available_currencies.mapped('name')
        if 'PEN' not in available_currency_names or "USD" not in available_currency_names:
            return result
        result['PEN'] = (1.0, fields.Date.context_today(self.with_context(tz='America/Lima')))
        url_format = "https://www.sunat.gob.pe/a/txt/tipoCambio.txt"
        try:
            res = requests.get(url_format, timeout=10)
            res.raise_for_status()
            line = res.text.splitlines()[0] or ""
        except Exception as e:
            _logger.error(e)
            return result
        sunat_value = line.split("|")
        try:
            rate = float(sunat_value[2])
        except ValueError as e:
            _logger.error(e)
            return result
        rate = 1.0 / rate if rate else 0
        date_rate_str = sunat_value[0]
        date_rate = datetime.datetime.strptime(date_rate_str, '%d/%m/%Y').strftime(DEFAULT_SERVER_DATE_FORMAT)
        result["USD"] = (rate, date_rate)
        return result

    def _parse_mindicador_data(self, available_currencies):
        """Parse function for mindicador.cl provider for Chile
        * Regarding needs of rates in Chile there will be one rate per day, except for UTM index (one per month)
        * The value of the rate is the "official" rate
        * The base currency is always CLP but with the inverse 1/rate.
        * The webservice returns the following currency rates:
            - EUR
            - USD (Dolar Observado)
            - UF (Unidad de Fomento)
            - UTM (Unidad Tributaria Mensual)
        """
        logger = _logger.getChild('mindicador')
        icp = self.env['ir.config_parameter'].sudo()
        server_url = icp.get_param('mindicador_api_url')
        if not server_url:
            server_url = 'https://mindicador.cl/api'
            icp.set_param('mindicador_api_url', server_url)
        foreigns = {
            "USD": "dolar",
            "EUR": "euro",
            "UF": "uf",
            "UTM": "utm",
        }
        available_currency_names = available_currencies.mapped('name')
        logger.debug('mindicador: available currency names: %s', available_currency_names)
        today_date = fields.Date.context_today(self.with_context(tz='America/Santiago'))
        rslt = {
            'CLP': (1.0, fields.Date.to_string(today_date)),
        }
        request_date = today_date.strftime('%d-%m-%Y')
        for index, currency in foreigns.items():
            if index not in available_currency_names:
                logger.debug('Index %s not in available currency name', index)
                continue
            url = server_url + '/%s/%s' % (currency, request_date)
            res = requests.get(url, timeout=30)
            res.raise_for_status()
            if 'html' in res.text:
                raise ValueError('Should be json')
            data_json = res.json()
            if not data_json['serie']:
                continue
            date = data_json['serie'][0]['fecha'][:10]
            rate = data_json['serie'][0]['valor']
            rslt[index] = (1.0 / rate,  date)
        return rslt

    def _parse_tcmb_data(self, available_currencies):
        """Parse function for Turkish Central bank provider
        * The webservice returns the following currency rates:
        - USD, AUD, DKK, EUR, GBP, CHF, SEK, CAD, KWD, NOK, SAR,
        - JPY, BGN, RON, RUB, IRR, CNY, PKR, QAR, KRW, AZN, AED
        """
        server_url = 'https://www.tcmb.gov.tr/kurlar/today.xml'
        available_currency_names = set(available_currencies.mapped('name'))

        # LegacyHTTPAdapter is used as connecting to the url raises an SSL error "unsafe legacy renegotiation disabled".
        # This happens with OpenSSL 3.0 when trying to connect to legacy websites that disable renegotiation without signaling it correctly.
        session = requests.Session()
        session.mount('https://', LegacyHTTPAdapter())

        res = session.get(server_url, timeout=30)
        res.raise_for_status()

        root = etree.fromstring(res.text.encode())
        rate_date = fields.Date.to_string(datetime.datetime.strptime(root.attrib['Date'], '%m/%d/%Y'))
        rslt = {
            currency.attrib['Kod']: (2 / (float(currency.find('ForexBuying').text) + float(currency.find('ForexSelling').text)), rate_date)
            for currency in root
            if currency.attrib['Kod'] in available_currency_names
        }
        rslt['TRY'] = (1.0, rate_date)

        return rslt

    def _parse_nbp_data(self, available_currencies):
        """ This method is used to update the currencies by using NBP (National Polish Bank) service API.
            Rates are given against PLN.
            Source: https://apps.odoo.com/apps/modules/14.0/trilab_live_currency_nbp/
            Code is mostly from Trilab's app with Trilab's permission.
        """

        # this is url to fetch active (at the moment of fetch) average currency exchange table
        request_url = 'https://api.nbp.pl/api/exchangerates/tables/{}/?format=json'
        requested_currency_codes = available_currencies.mapped('name')
        result = {}

        # there are 3 tables with currencies:
        #   A - most used ones average,
        #   B - exotic currencies average,
        #   C - common bid/sell
        # we will parse first one and if there are unmatched currencies, proceed with second one

        for table_type in ['A', 'B']:
            if not requested_currency_codes:
                break

            response = requests.get(request_url.format(table_type), timeout=10)
            response.raise_for_status()
            response_data = response.json()
            for exchange_table in response_data:
                # there *should not be* be more than one table in response, but let's be on the safe side
                # and parse this in a loop as response is a list

                # effective date of this table
                table_date = datetime.datetime.strptime(
                    exchange_table['effectiveDate'], '%Y-%m-%d'
                ).date()

                # for tax purpose, polish companies must use rate of day before transaction
                # this is achieved by offsetting the rate date by one day
                table_date += relativedelta(days=1)

                # add base currency
                if 'PLN' not in result and 'PLN' in requested_currency_codes:
                    result['PLN'] = (1.0, table_date)

                for rec in exchange_table['rates']:
                    if rec['code'] in requested_currency_codes:
                        result[rec['code']] = (1.0 / rec['mid'], table_date)
                        requested_currency_codes.remove(rec['code'])

        return result

    @api.model
    def run_update_currency(self):
        """ This method is called from a cron job to update currency rates.
        """
        records = self.search([('currency_next_execution_date', '<=', fields.Date.today())])
        if records:
            to_update = self.env['res.company']
            for record in records:
                if record.currency_interval_unit == 'daily':
                    next_update = relativedelta(days=+1)
                elif record.currency_interval_unit == 'weekly':
                    next_update = relativedelta(weeks=+1)
                elif record.currency_interval_unit == 'monthly':
                    next_update = relativedelta(months=+1)
                else:
                    record.currency_next_execution_date = False
                    continue
                record.currency_next_execution_date = datetime.date.today() + next_update
                to_update += record
            to_update.with_context(suppress_errors=True).update_currency_rates()


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    currency_interval_unit = fields.Selection(related="company_id.currency_interval_unit", readonly=False)
    currency_provider = fields.Selection(related="company_id.currency_provider", readonly=False)
    currency_next_execution_date = fields.Date(related="company_id.currency_next_execution_date", readonly=False)

    @api.onchange('currency_interval_unit')
    def onchange_currency_interval_unit(self):
        #as the onchange is called upon each opening of the settings, we avoid overwriting
        #the next execution date if it has been already set
        if self.company_id.currency_next_execution_date:
            return
        if self.currency_interval_unit == 'daily':
            next_update = relativedelta(days=+1)
        elif self.currency_interval_unit == 'weekly':
            next_update = relativedelta(weeks=+1)
        elif self.currency_interval_unit == 'monthly':
            next_update = relativedelta(months=+1)
        else:
            self.currency_next_execution_date = False
            return
        self.currency_next_execution_date = datetime.date.today() + next_update

    def update_currency_rates_manually(self):
        self.ensure_one()

        if not (self.company_id.update_currency_rates()):
            raise UserError(_('Unable to connect to the online exchange rate platform. The web service may be temporary down. Please try again in a moment.'))
