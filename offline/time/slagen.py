#!/usr/bin/env python
# Generate SLAS from a forecast
import argparse
import datetime
import logging
import os
import os.path
import subprocess
import sys

import numpy as np
import pandas as pd

from ..core.sla import Sla, SlaNodeSpec
from ..pricing.generator import price_slas
from ..time.SLA3D import get_tse, chunk_series_as_sla
from ..time.disc_plot import plot_forecast_and_disc_and_total
from ..time.persistence import Session

TIME_PATH = os.path.dirname(os.path.realpath(__file__))

RESULTS_FOLDER = os.path.join(os.path.dirname(os.path.realpath(__file__)), '../results')
DATA_FOLDER = os.path.join(os.path.dirname(os.path.realpath(__file__)), '../data')


def get_forecast_from_date(df):
    dff = df["fcmean"] - df["fc0"]
    return df["Index"].values[-df.index[len(df) - len(dff[dff == 0])]]


def discretize(windows, centroids, ts, df, forecast_detector=get_forecast_from_date):
    ts_forecasts = ts[forecast_detector(df):]
    return get_tse(ts_forecasts, windows, centroids)


import tempfile


def get_forecast(file, force_refresh=False):
    file = os.path.abspath(file)
    out_file = os.path.abspath(file + ".forecast")
    logging.debug(" forecast already exist? %s" % os.path.isfile(out_file))
    if force_refresh or not os.path.isfile(out_file):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            df = pd.read_csv(file, names=["time", "values"])
            ts = pd.Series(df["values"].values, index=pd.to_datetime(df["time"]))
            resampled = ts.resample("1H").mean().bfill()
            resampled.to_csv(f)

        subprocess.call(["%s/compute_forecast.R" % TIME_PATH, "-i", "%s" % f.name, "-o", out_file], cwd=TIME_PATH,
                        # stdout=open(os.devnull, 'wb'), stderr=open(os.devnull, 'wb')
                        )
    else:
        logging.debug("file %s is already present! no need to re-gerenate" % out_file)

    with open(out_file, "r") as f:
        df = pd.read_csv(f)
    ts = pd.Series(data=df["fcmean"].values,
                   index=df.apply(lambda row: datetime.datetime.strptime(row['Index'], '%Y-%m-%d %H:%M:%S'),
                                  axis=1).values)

    return file, ts, df


class SlaPricerWrapper:
    pricer = price_slas


def fill_db_with_sla(data_files, pricer, tenant, **kwargs):
    '''

    :param file: the file to read the data from
    :param windows: the size of the window for smoothing
    :param centroids: the number of threshold value to detec
    :param random: if True, new random data will be generated
    :return: a tuple containing :
              ts: the generated time serie and its forecast, as reccorded in the table
              date_start: the date of the first observation for the serie
              date_start_forecast: the date of the first forecast
              date_end_forecast: the date of the last forecast
    '''
    session = Session()

    start_nodes = [node.id for node in kwargs.get("start_nodes", [])]
    cdn_nodes = [node.id for node in kwargs.get("cdn_nodes", [])]
    forecast_series_count = len(start_nodes)

    file_to_node = dict(list(zip(data_files, start_nodes)))

    best_price = sys.float_info.max
    best_slas = None
    best_discretization_parameter = None
    best_tse = None

    tsdf = {file: get_forecast(os.path.join(DATA_FOLDER, file)) for file in
            data_files[0:forecast_series_count]}

    for windows in range(1, 11, 2):
        for centroids in range(1, 11, 1):
            tses = {key: discretize(windows, centroids, ts=value[1], df=value[2]) for key, value in list(tsdf.items())}
            slas = chunk_series_as_sla(tses)
            price = pricer([item for sublist in list(slas.values()) for item in sublist])
            logging.debug("%d slas generated for (%d,%d)" % (
            sum([1 for sublist in list(slas.values()) for item in sublist]), windows, centroids))

            logging.debug("For (%d,%d) the price is %lf" % (windows, centroids, price))
            if price < best_price:
                best_tse = tses
                best_price = price
                best_slas = slas
                best_discretization_parameter = (windows, centroids)
                logging.debug("(%d,%d) is the best candidate to far" % (windows, centroids))

    logging.info(
        "best discretization parameters are %s with a price of %ld " % (str(best_discretization_parameter), best_price))

    total_sla_plot = pd.Series()
    for item in [item for sublist in list(best_slas.values()) for item in sublist]:
        total_sla_plot = pd.Series.add(item, total_sla_plot, fill_value=0)

    logging.info("generating     %d slas for best solution" % (
        sum([1 for sublist in list(best_slas.values()) for item in sublist]),))

    for key, sla_list in list(best_slas.items()):

        for topokey, value in [(file_to_node[key], value) for value in sla_list]:
            nodespecs = []
            ns = SlaNodeSpec(toponode_id=topokey, type="start", attributes={"bandwidth": np.mean(value)})
            session.add(ns)
            nodespecs.append(ns)
            total_sla_plot = pd.Series.add(total_sla_plot, value, fill_value=0)

            nodespecs += [SlaNodeSpec(toponode_id=cdn_node, type="cdn") for cdn_node in cdn_nodes]

            sla_instance = Sla(start_date=pd.to_datetime(value.index[0]), end_date=pd.to_datetime(value.index[-1]),
                               tenant_id=tenant.id,
                               sla_node_specs=nodespecs,
                               substrate=kwargs.get("substrate", None),
                               delay=kwargs.get("delay", 50)
                               )
            session.add(sla_instance)
            session.flush()

    plot_forecast_and_disc_and_total(tsdf, best_discretization_parameter[0], best_discretization_parameter[1],
                                     out_file_name="dummy" + ".svg", plot_name=None, total_sla_plot=total_sla_plot)

    return list(best_tse.values())[0].index[0], list(best_tse.values())[0].index[
        -1], best_price, best_discretization_parameter, len(list(best_slas.keys()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='1 iteration for solver')
    parser.add_argument('--file', "-f", default="forecast.csv", type=str)
    parser.add_argument('--windows', "-w", default=5, type=int)
    parser.add_argument('--centroids', "-c", default=5, type=int)
    args = parser.parse_args()
    fill_db_with_sla(file=args.file, windows=args.windows, centroids=args.centroids)
