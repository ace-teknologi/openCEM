"""Multi year simulation module"""
__author__ = "José Zapata"
__copyright__ = "Copyright 2018, ITP Renewables, Australia"
__credits__ = ["José Zapata", "Dylan McConnell", "Navid Hagdadi"]
__license__ = "GPLv3"
__version__ = "0.9.2"
__maintainer__ = "José Zapata"
__email__ = "jose.zapata@itpau.com.au"
__status__ = "Development"
import configparser
import datetime
import json
import os.path
import tempfile

import pandas as pd
from pyomo.opt import SolverFactory

import cemo.const
from cemo.cluster import ClusterRun, InstanceCluster
from cemo.jsonify import json_carry_forward_cap, jsonify
from cemo.model import create_model
from cemo.utils import printstats


def sqllist(techset):
    """Generate a technology set for SQL statement"""
    out = []
    for i in techset.keys():
        for j in techset[i]:
            out.append((i, j))
    if not out:
        out.append((99, 99))  # preserve query syntax if list is empty
    return "(" + ", ".join(map(str, out)) + ")"


def dclist(techset):
    """Generate a technology set for a data command statement"""
    out = ""
    for i in techset.keys():
        for j in techset[i]:
            out = out + str(i) + " " + str(j) + "\n"
    return out


def roundup(cap):
    '''
    Round results to 2 signigicant digits.
    Catching small negative numbers due to solver numerical tolerance.
    Let big negative numners pass to raise exception.
    '''
    if cap > -1e-6 and cap < 0:
        return 0
    return round(cap, 2)


def setinstancecapacity(instance, clustercap):
    ''' write cluster gen_cap_op results for instance'''
    data = clustercap.data
    for z in instance.zones:
        for n in instance.gen_tech_per_zone[z]:
            key = str(z) + ',' + str(n)
            instance.gen_cap_new[z, n] = roundup(
                data['gen_cap_new[' + key + ']']['solution'])
        for s in instance.stor_tech_per_zone[z]:
            key = str(z) + ',' + str(s)
            instance.stor_cap_new[z, s] = roundup(
                data['stor_cap_new[' + key + ']']['solution'])
        for h in instance.hyb_tech_per_zone[z]:
            key = str(z) + ',' + str(h)
            instance.hyb_cap_new[z, h] = roundup(
                data['hyb_cap_new[' + key + ']']['solution'])
        for r in instance.retire_gen_tech_per_zone[z]:
            key = str(z) + ',' + str(r)
            instance.gen_cap_ret[z, r] \
                = roundup(data['gen_cap_ret[' + key + ']']['solution'])

    instance.gen_cap_new.fix()
    instance.stor_cap_new.fix()
    instance.hyb_cap_new.fix()
    instance.gen_cap_ret.fix()
    return instance


class SolveTemplate:
    """Solve Multi year openCEM simulation based on template"""

    def __init__(self, cfgfile, solver='cbc', log=False, tmpdir=tempfile.mkdtemp() + '/'):
        config = configparser.ConfigParser()
        try:
            with open(cfgfile) as f:
                config.read_file(f)
            self.cfgfile = cfgfile
        except FileNotFoundError:
            raise FileNotFoundError('openCEM Scenario config file not found')

        Scenario = config['Scenario']
        self.Name = Scenario['Name']
        self.Years = json.loads(Scenario['Years'])
        # Read policy constraints from config file
        self.nem_ret_ratio = None
        if config.has_option('Scenario', 'nem_ret_ratio'):
            self.nem_ret_ratio = json.loads(Scenario['nem_ret_ratio'])
        self.nem_ret_gwh = None
        if config.has_option('Scenario', 'nem_ret_gwh'):
            self.nem_ret_gwh = json.loads(Scenario['nem_ret_gwh'])
        self.region_ret_ratio = None
        if config.has_option('Scenario', 'region_ret_ratio'):
            self.region_ret_ratio = dict(
                json.loads(Scenario['region_ret_ratio']))
        self.emitlimit = None
        if config.has_option('Scenario', 'emitlimit'):
            self.emitlimit = json.loads(Scenario['emitlimit'])
        self.nem_disp_ratio = None
        if config.has_option('Scenario', 'nem_disp_ratio'):
            self.nem_disp_ratio = json.loads(Scenario['nem_disp_ratio'])
        self.nem_re_disp_ratio = None
        if config.has_option('Scenario', 'nem_re_disp_ratio'):
            self.nem_re_disp_ratio = json.loads(Scenario['nem_re_disp_ratio'])
        # Keep track of policy options to configure model instances down the line
        self.model_options = {
            'nem_ret_ratio': (True if self.nem_ret_ratio is not None else False),
            'nem_ret_gwh': (True if self.nem_ret_gwh is not None else False),
            'region_ret_ratio': (True if self.region_ret_ratio is not None else False),
            'emitlimit': (True if self.emitlimit is not None else False),
            'nem_disp_ratio': (True if self.nem_disp_ratio is not None else False),
            'nem_re_disp_ratio': (True if self.nem_re_disp_ratio is not None else False),
        }

        self.discountrate = Scenario['discountrate']
        self.cost_emit = None
        if config.has_option('Scenario', 'cost_emit'):
            self.cost_emit = json.loads(Scenario['cost_emit'])
        # Miscelaneous options
        self.description = None
        if config.has_option('Scenario', 'Description'):
            self.description = Scenario['Description']
        # Advanced configuration options
        Advanced = config['Advanced']
        self.Template = Advanced['Template']

        self.custom_costs = None
        if config.has_option('Advanced', 'custom_costs'):
            self.custom_costs = Advanced['custom_costs']

        self.exogenous_capacity = None
        if config.has_option('Advanced', 'exogenous_capacity'):
            self.exogenous_capacity = Advanced['exogenous_capacity']

        self.cluster = Advanced.getboolean('cluster')

        self.cluster_max_d = int(Advanced['cluster_sets'])

        self.regions = cemo.const.REGION.keys()
        if config.has_option('Advanced', 'regions'):
            self.regions = json.loads(Advanced['regions'])

        self.zones = cemo.const.ZONE.keys()
        if config.has_option('Advanced', 'zones'):
            self.zones = json.loads(Advanced['zones'])

        self.all_tech = cemo.const.TECH_TYPE.keys()
        if config.has_option('Advanced', 'all_tech'):
            self.all_tech = json.loads(Advanced['all_tech'])
        self.all_tech_per_zone = dict(json.loads(Advanced['all_tech_per_zone']))

        self.tmpdir = tmpdir
        self.solver = solver
        self.log = log
        # initialisation functions
        self.tracetechs()  # TODO refactor this

    # Validate configuration file entries before continuing
    @property
    def Years(self):
        return self._Years

    @Years.setter
    def Years(self, y):
        if max(y) > 2050:
            raise ValueError("openCEM-Years: Last full year of data is fy2050")
        if min(y) < 2018:
            raise Exception("openCEM-Years: No historical data available")
        self._Years = sorted(y)

    @property
    def discountrate(self):
        return self._discountrate

    @discountrate.setter
    def discountrate(self, data):
        if float(data) < 0 or float(data) > 1:
            raise ValueError(
                'openCEM-discountrate: Value must be between 0 and 1')
        self._discountrate = data

    @property
    def cost_emit(self):
        return self._cost_emit

    @cost_emit.setter
    def cost_emit(self, data):
        if data is not None:
            if len(data) != len(self.Years):
                raise ValueError(
                    'openCEM-cost_emit: List length does not match Years list')
            if any(x < 0 for x in data):
                raise ValueError(
                    'openCEM-cost_emit: Value must be greater than 0')
        self._cost_emit = data

    @property
    def nem_ret_ratio(self):
        return self._nem_ret_ratio

    @nem_ret_ratio.setter
    def nem_ret_ratio(self, data):
        if data is not None:
            if len(data) != len(self.Years):
                raise ValueError(
                    'openCEM-nem_ret_ratio: List length does not match Years list')
            if any(x < 0 for x in data) or any(x > 1 for x in data):
                raise ValueError(
                    'openCEM-nem_ret_ratio: List element(s) outside range [0,1]')
        self._nem_ret_ratio = data

    @property
    def nem_ret_gwh(self):
        return self._nem_ret_gwh

    @nem_ret_gwh.setter
    def nem_ret_gwh(self, data):
        if data is not None:
            if len(data) != len(self.Years):
                raise ValueError(
                    'openCEM-nem_ret_gwh: List length does not match Years list')
            if any(x < 0 for x in data):
                raise ValueError(
                    'openCEM-nem_ret_gwh: List element(s) outside range [0,1]')
        self._nem_ret_gwh = data

    @property
    def region_ret_ratio(self):
        return self._region_ret_ratio

    @region_ret_ratio.setter
    def region_ret_ratio(self, data):
        if data is not None:
            for d in data:
                if len(data[d]) != len(self.Years):
                    raise ValueError(
                        'openCEM-region_ret_ratio: List %s length does not match Years list'
                        % d)
                if any(x < 0 for x in data[d]) or any(x > 1 for x in data[d]):
                    raise ValueError(
                        'openCEM-region_ret_ratio: Element(s) in list %s outside range [0,1]'
                        % d)
        self._region_ret_ratio = data

    @property
    def emitlimit(self):
        return self._emitrate

    @emitlimit.setter
    def emitlimit(self, data):
        if data is not None:
            if len(data) != len(self.Years):
                raise ValueError(
                    'openCEM-emitlimit: List %s length does not match Years list'
                )
            if any(x < 0 for x in data):
                raise ValueError(
                    'openCEM-emitlimit: Element(s) in list must be positive')
        self._emitrate = data

    @property
    def nem_disp_ratio(self):
        return self._nem_disp_ratio

    @nem_disp_ratio.setter
    def nem_disp_ratio(self, data):
        if data is not None:
            if len(data) != len(self.Years):
                raise ValueError(
                    'openCEM-nem_disp_ratio: List %s length does not match Years list'
                )
            if any(x < 0 for x in data) or any(x > 1 for x in data):
                raise ValueError(
                    'openCEM-nem_disp_ratio: Element(s) in list must be between 0 and 1')
        self._nem_disp_ratio = data

    @property
    def nem_re_disp_ratio(self):
        return self._nem_re_disp_ratio

    @nem_re_disp_ratio.setter
    def nem_re_disp_ratio(self, data):
        if data is not None:
            if len(data) != len(self.Years):
                raise ValueError(
                    'openCEM-nem_re_disp_ratio: List %s length does not match Years list'
                )
            if any(x < 0 for x in data) or any(x > 1 for x in data):
                raise ValueError(
                    'openCEM-nem_re_disp_ratio: Element(s) in list must be between 0 and 1')
        self._nem_re_disp_ratio = data

    @property
    def Template(self):
        return self._Template

    @Template.setter
    def Template(self, a):
        if not os.path.isfile(a):
            raise OSError("openCEM-Template: File not found")
        self._Template = a

    @property
    def custom_costs(self):
        return self._custom_costs

    @custom_costs.setter
    def custom_costs(self, a):
        if a is not None:
            if not os.path.isfile(a):
                raise OSError("openCEM-custom_costs: File not found")
        self._custom_costs = a

    @property
    def exogenous_capacity(self):
        return self._exogenous_capacity

    @exogenous_capacity.setter
    def exogenous_capacity(self, a):
        if a is not None:
            if not os.path.isfile(a):
                raise OSError("openCEM-exogenous_capacity: File not found")
        self._exogenous_capacity = a

    def tracetechs(self):  # TODO refactor this and how tech sets populate template
        self.fueltech = {}
        self.committech ={}
        self.regentech = {}
        self.dispgentech = {}
        self.redispgentech = {}
        self.hybtech = {}
        self.gentech = {}
        self.stortech = {}
        self.retiretech = {}
        for i in self.all_tech_per_zone:
            self.fueltech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.FUEL_TECH]
            })
            self.committech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.COMMIT_TECH]
            })
            self.regentech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.RE_GEN_TECH]
            })
            self.dispgentech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.DISP_GEN_TECH]
            })
            self.redispgentech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.RE_DISP_GEN_TECH]
            })
            self.hybtech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.HYB_TECH]
            })
            self.gentech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.GEN_TECH]
            })
            self.stortech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.STOR_TECH]
            })
            self.retiretech.update({
                i: [j for j in self.all_tech_per_zone[i] if j in cemo.const.RETIRE_TECH]
            })

    def carryforwardcap(self, year):
        if self.Years.index(year):
            prevyear = self.Years[self.Years.index(year) - 1]
            opcap0 = "load '" + self.tmpdir + "gen_cap_op" + \
                str(prevyear) + \
                ".json' : [zones,all_tech] gen_cap_initial stor_cap_initial hyb_cap_initial;"
        else:
            opcap0 = '''#operating capacity for all technilogies and regions
load "opencem.ckvu5hxg6w5z.ap-southeast-1.rds.amazonaws.com" database=opencem_input
user=select password=select_password using=pymysql
query="select ntndp_zone_id as zones, technology_type_id as all_tech, sum(reg_cap) as gen_cap_initial
from capacity
where (ntndp_zone_id,technology_type_id) in
''' + sqllist(self.gentech) + '''
and commissioning_year is NULL
group by zones,all_tech;" : [zones,all_tech] gen_cap_initial;

# operating capacity for all technilogies and regions
load "opencem.ckvu5hxg6w5z.ap-southeast-1.rds.amazonaws.com" database=opencem_input
user=select password=select_password using=pymysql
query="select ntndp_zone_id as zones, technology_type_id as all_tech, sum(reg_cap) as stor_cap_initial
from capacity
where (ntndp_zone_id,technology_type_id) in
''' + sqllist(self.stortech) + '''
and commissioning_year is NULL
group by zones,all_tech;" : [zones,all_tech] stor_cap_initial;

# operating capacity for all technilogies and regions
load "opencem.ckvu5hxg6w5z.ap-southeast-1.rds.amazonaws.com" database=opencem_input
user=select password=select_password using=pymysql
query="select ntndp_zone_id as zones, technology_type_id as all_tech, sum(reg_cap) as hyb_cap_initial
from capacity
where (ntndp_zone_id,technology_type_id) in
''' + sqllist(self.hybtech) + '''
and commissioning_year is NULL
group by zones,all_tech;" : [zones,all_tech] hyb_cap_initial;
'''
        return opcap0

    def carry_forward_cap_costs(self, year):
        '''Save total annualised capital costs in carry forward json'''
        carry_fwd_cost = ''
        if self.Years.index(year):
            carry_fwd_cost = "#Carry forward annualised capital costs\n"
            prevyear = self.Years[self.Years.index(year) - 1]
            carry_fwd_cost += "load '" + self.tmpdir + "gen_cap_op" + \
                str(prevyear) + \
                ".json' : cost_cap_carry_forward;\n"
        return carry_fwd_cost

    def produce_custom_costs(self, y):
        year = str(y)
        custom_costs = '\n'
        keywords = {
            'cost_gen_build': 'zonetech',
            'cost_hyb_build': 'zonetech',
            'cost_stor_build': 'zonetech',
            'cost_fuel': 'zonetech',
            'cost_gen_fom': 'tech',
            'cost_gen_vom': 'tech',
            'cost_hyb_fom': 'tech',
            'cost_hyb_vom': 'tech',
            'cost_stor_fom': 'tech',
            'cost_stor_vom': 'tech'}
        if self.custom_costs is not None:
            costs = pd.read_csv(self.custom_costs, skipinitialspace=True)
            for key in keywords.keys():
                if year in costs.columns:
                    cost = costs[
                        (costs['name'] == key) &
                        (costs['tech'].isin(self.all_tech)
                         & (costs['zone'].isin(self.zones)))
                    ].dropna(subset=[year])
                else:
                    cost = pd.DataFrame()
                if not cost.empty:
                    custom_costs += '#Custom cost entry for ' + key + '\n'
                    custom_costs += 'param ' + key + ':=\n'
                    if keywords[key] == 'tech':
                        custom_costs += cost[['tech', year]].to_string(header=False,
                                                                       index=False,
                                                                       formatters={
                                                                           'tech': lambda x: '%i' % x,
                                                                           year: lambda x: '%10.2f' % x,
                                                                       })
                    else:
                        custom_costs += cost[['zone', 'tech', year]
                                             ].to_string(header=False, index=False,
                                                         formatters={
                                                             'zone': lambda x: '%i' % x,
                                                             'tech': lambda x: '%i' % x,
                                                             year: lambda x: '%10.2f' % x,
                                                         })
                    custom_costs += '\n;\n'

        return custom_costs

    def produce_exogenous_capacity(self, year):
        exogenous_capacity = '\n'
        keywords = {
            'gen_cap_exo': 'zonetech',
            'stor_cap_exo': 'zonetech',
            'hyb_cap_exo': 'zonetech',
            'ret_gen_cap_exo': 'zonetech',
        }
        if self.exogenous_capacity is not None:
            capacity = pd.read_csv(self.exogenous_capacity, skipinitialspace=True)
            prevyear = self.Years[self.Years.index(year) - 1]
            for key in keywords.keys():
                cap = capacity[
                    (capacity['year'] > int(prevyear)) &
                    (capacity['year'] <= int(year)) &
                    (capacity['name'] == key) &
                    (capacity['tech'].isin(self.all_tech)) &
                    (capacity['zone'].isin(self.zones))
                ]
                if not cap.empty:
                    exogenous_capacity += '#Exogenous capacity entry ' + key + '\n'
                    exogenous_capacity += 'param ' + key + ':=\n'
                    exogenous_capacity += cap[['zone', 'tech', 'value']
                                              ].to_string(header=False, index=False)
                    exogenous_capacity += '\n;\n'

        return exogenous_capacity

    def generateyeartemplate(self, year, test=False):
        """Generate data command file template used for clusters and full runs"""
        date1 = datetime.datetime(year-1, 7, 1, 0, 0, 0)
        strd1 = "'" + str(date1) + "'"
        date2 = datetime.datetime(year, 6, 30, 23, 0, 0)
        if test:
            date2 = datetime.datetime(year-1, 7, 3, 23, 0, 0)
        strd2 = "'" + str(date2) + "'"
        drange = "BETWEEN " + strd1 + " AND " + strd2
        dcfName = self.tmpdir + 'Sim' + str(year) + '.dat'
        fcr = "\n#Discount rate for project\n"\
            + "param all_tech_discount_rate := " + \
            str(self.discountrate) + ";\n"

        opcap0 = self.carryforwardcap(year)
        carry_fwd_cap = self.carry_forward_cap_costs(year)
        custom_costs = self.produce_custom_costs(year)
        exogenous_capacity = self.produce_exogenous_capacity(year)

        cemit = ""
        if self.cost_emit:
            cemit = "#Cost of emissions $/Mhw\n"\
                + "param cost_emit:= " + str(self.cost_emit[self.Years.index(year)]) + ";\n"

        nem_ret_ratio = ""
        if self.nem_ret_ratio:
            nem_ret_ratio = "\n # NEM wide RET\n"\
                + "param nem_ret_ratio :=" + \
                str(self.nem_ret_ratio[self.Years.index(year)]) + ";\n"

        nem_ret_gwh = ""
        if self.nem_ret_gwh:
            nem_ret_gwh = "\n # NEM wide RET\n"\
                + "param nem_ret_gwh :=" + \
                str(self.nem_ret_gwh[self.Years.index(year)]) + ";\n"

        region_ret_ratio = ""
        if self.region_ret_ratio:
            region_ret_ratio = "\n #Regional based RET\n"\
                + "param region_ret_ratio := " +\
                ' '.join(str(i) + " " + str(self.region_ret_ratio[i][self.Years.index(year)])
                         for i in self.region_ret_ratio) + ";\n"

        emitlimit = ""
        if self.emitlimit:
            emitlimit = "\n #NEM wide emission limit (in GT)\n"\
                + "param nem_year_emit_limit := " +\
                  str(self.emitlimit[self.Years.index(year)]) + ";\n"

        nem_disp_ratio = ""
        if self.nem_disp_ratio:
            nem_disp_ratio = "\n #NEM wide minimum generation ratio from dispatchable tech\n"\
                + "param nem_disp_ratio := " +\
                  str(self.nem_disp_ratio[self.Years.index(year)]) + ";\n"

        nem_re_disp_ratio = ""
        if self.nem_re_disp_ratio:
            nem_re_disp_ratio = "\n #NEM wide minimum generation ratio from dispatchable tech\n"\
                + "param nem_re_disp_ratio := " +\
                  str(self.nem_re_disp_ratio[self.Years.index(year)]) + ";\n"

        if self.Years.index(year) == 0:
            prevyear = 2017
        else:
            prevyear = self.Years[self.Years.index(year) - 1]

        with open(self.Template, 'rt') as fin:
            with open(dcfName, 'w') as fo:
                for line in fin:
                    line = line.replace('[regions]', " ".join(str(i) for i in self.regions))
                    line = line.replace('[zones]', " ".join(str(i) for i in self.zones))
                    line = line.replace('[alltech]', " ".join(str(i) for i in self.all_tech))
                    line = line.replace('XXXX', str(year))
                    line = line.replace('WWWW', str(prevyear))
                    line = line.replace('[gentech]', dclist(self.gentech))
                    line = line.replace('[gentechdb]', sqllist(self.gentech))
                    line = line.replace(
                        '[gentechlist]', ", ".join(
                            str(i) for i in cemo.const.GEN_TECH if i in self.all_tech))
                    line = line.replace('[stortech]', dclist(self.stortech))
                    line = line.replace('[stortechdb]', sqllist(self.stortech))
                    line = line.replace(
                        '[stortechlist]', ", ".join(
                            str(i) for i in cemo.const.STOR_TECH if i in self.all_tech))
                    line = line.replace('[hybtech]', dclist(self.hybtech))
                    line = line.replace('[hybtechdb]', sqllist(self.hybtech))
                    line = line.replace(
                        '[hybtechlist]', ", ".join(
                            str(i) for i in cemo.const.HYB_TECH if i in self.all_tech))
                    line = line.replace('[retiretech]',
                                        dclist(self.retiretech))
                    line = line.replace('[retiretechdb]',
                                        sqllist(self.retiretech))
                    line = line.replace(
                        '[retiretechset]', " ".join(
                            str(i) for i in cemo.const.RETIRE_TECH))
                    line = line.replace('[fueltech]', dclist(self.fueltech))
                    line = line.replace('[fueltechdb]', sqllist(self.fueltech))
                    line = line.replace(
                        '[fueltechset]', " ".join(
                            str(i) for i in cemo.const.FUEL_TECH))
                    line = line.replace('[committech]', dclist(self.committech))
                    line = line.replace('[regentech]', dclist(self.regentech))
                    line = line.replace('[dispgentech]', dclist(self.dispgentech))
                    line = line.replace('[redispgentech]', dclist(self.redispgentech))
                    line = line.replace(
                        '[stortechset]', " ".join(
                            str(i) for i in cemo.const.STOR_TECH))
                    line = line.replace(
                        '[hybtechset]', " ".join(
                            str(i) for i in cemo.const.HYB_TECH))
                    line = line.replace(
                        '[nobuildset]', " ".join(
                            str(i) for i in cemo.const.NOBUILD_TECH))
                    line = line.replace('[carryforwardcap]', opcap0)
                    line = line.replace('[timerange]', drange)
                    # REVIEW [___techset] entrie may be superceeded by model sets initialisation
                    fo.write(line)
                fo.write(custom_costs)
                fo.write(exogenous_capacity)
                fo.write(fcr)
                fo.write(cemit)
                fo.write(carry_fwd_cap)
                fo.write(nem_ret_ratio)
                fo.write(nem_ret_gwh)
                fo.write(region_ret_ratio)
                fo.write(emitlimit)
                fo.write(nem_disp_ratio)
                fo.write(nem_re_disp_ratio)
        return dcfName

    def solve(self):
        """
        Multi year simulation:
        Instantiate a template instance for each year in the simulation.
        Calcualte capacity using clustering and dispatch with full year.
        Alternatively caculate capacity and dispatch simultanteously using full year instance
        Save capacity results for carry forward in json file.
        Save full results for year in JSON file.
        Assemble full simulation output as metadata+ full year results in each simulated year
        """
        for y in self.Years:
            if self.log:
                print("openCEM multi: Starting simulation for year %s" % y)
            # Populate template with this inv period's year and timestamps
            year_template = self.generateyeartemplate(y)
            # Solve full year capacity and dispatch instance
            # Create model based on policy configuration options
            model = create_model(
                y,
                nem_ret_ratio=self.model_options['nem_ret_ratio'],
                nem_ret_gwh=self.model_options['nem_ret_gwh'],
                region_ret_ratio=self.model_options['region_ret_ratio'],
                emitlimit=self.model_options['emitlimit'],
                nem_disp_ratio=self.model_options['nem_disp_ratio'],
                nem_re_disp_ratio=self.model_options['nem_re_disp_ratio'])
            # create model instance based in template data
            inst = model.create_instance(year_template)
            # These presolve capacity on a clustered form
            if self.cluster:
                clus = InstanceCluster(inst, self.cluster_max_d)
                ccap = ClusterRun(
                    clus,
                    year_template,
                    model_options=self.model_options,
                    solver=self.solver,
                    log=self.log).run_cluster()
                inst = setinstancecapacity(inst, ccap)

            # Solve the model (or just dispatch if capacity has been solved)
            opt = SolverFactory(self.solver)
            if self.log:
                print("openCEM multi: Starting full year dispatch simulation")
            opt.solve(inst, tee=self.log, keepfiles=self.log)

            # Carry forward operating capacity to next Inv period
            opcap = json_carry_forward_cap(inst)
            if y != self.Years[-1]:
                with open(self.tmpdir + 'gen_cap_op' + str(y) + '.json',
                          'w') as op:
                    json.dump(opcap, op)
            # Dump simulation result in JSON forma
            if self.log:
                print("openCEM multi: Saving year %s results into temporary file" % y)
            out = jsonify(inst)
            with open(self.tmpdir + str(y) + '.json', 'w') as jo:
                json.dump(out, jo)

            printstats(inst)

            del inst  # to keep memory down
        # Merge JSON output for all investment periods
        if self.log:
            print("openCEM multi: Saving final results to JSON file")
        self.mergejsonyears()

    def mergejsonyears(self):
        '''Merge the full year JSON output for each simulated year in a single dictionary'''
        data = self.generate_metadata()
        for y in self.Years:
            with open(self.tmpdir + str(y) + '.json', 'r') as f:
                yeardata = {str(y): json.load(f)}
            data.update(yeardata)
        # Save json output named after .cfg file
        with open(self.cfgfile.split(".")[0] + '.json', 'w') as fo:
            json.dump(data, fo)

    def generate_metadata(self):
        '''Append simulation metadata to full JSON output'''
        meta = {
            "Name": self.Name,
            "Years": self.Years,
            "Template": self.Template,
            "Clustering": self.cluster,
            "Cluster_number":  self.cluster_max_d if self.cluster else "N/A",
            "Solver": self.solver,
            "Discount_rate": self.discountrate,
            "Emission_cost": self.cost_emit,
            "Description": self.description,
            "NEM wide RET as ratio": self.nem_ret_ratio,
            "NEM wide RET as GWh": self.nem_ret_gwh,
            "Regional based RET": self.region_ret_ratio,
            "System emission limit": self.emitlimit,
            "Dispatchable generation ratio ": self.nem_disp_ratio,
            "Renewable Dispatchable generation ratio ": self.nem_re_disp_ratio,
            "Custom costs": pd.read_csv(self.custom_costs).to_dict(orient='records') if self.custom_costs is not None else None,
            "Exogenous Capacity decisions": pd.read_csv(self.exogenous_capacity).to_dict(orient='records') if self.exogenous_capacity is not None else None,
        }

        return {'meta': meta}
